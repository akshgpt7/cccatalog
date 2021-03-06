import logging
import json
from textwrap import dedent
from airflow.hooks.postgres_hook import PostgresHook
from util.loader import column_names as col
from util.loader import provider_details as prov
from psycopg2.errors import InvalidTextRepresentation

logger = logging.getLogger(__name__)

LOAD_TABLE_NAME_STUB = 'provider_image_data'
IMAGE_TABLE_NAME = 'image'
DB_USER_NAME = 'deploy'
NOW = 'NOW()'
FALSE = "'f'"


def create_loading_table(
        postgres_conn_id,
        identifier
):
    """
    Create intermediary table and indices if they do not exist
    """
    load_table = _get_load_table_name(identifier)
    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    postgres.run(
        dedent(
            f'''
            CREATE TABLE public.{load_table} (
              {col.FOREIGN_ID} character varying(3000),
              {col.LANDING_URL} character varying(1000),
              {col.DIRECT_URL} character varying(3000),
              {col.THUMBNAIL} character varying(3000),
              {col.WIDTH} integer,
              {col.HEIGHT} integer,
              {col.FILESIZE} integer,
              {col.LICENSE} character varying(50),
              {col.LICENSE_VERSION} character varying(25),
              {col.CREATOR} character varying(2000),
              {col.CREATOR_URL} character varying(2000),
              {col.TITLE} character varying(5000),
              {col.META_DATA} jsonb,
              {col.TAGS} jsonb,
              {col.WATERMARKED} boolean,
              {col.PROVIDER} character varying(80),
              {col.SOURCE} character varying(80),
              {col.INGESTION_TYPE} character varying(80)
            );
            '''
        )
    )
    postgres.run(
        f'ALTER TABLE public.{load_table} OWNER TO {DB_USER_NAME};'
    )
    postgres.run(
        dedent(
            f'''
            CREATE INDEX IF NOT EXISTS {load_table}_{col.PROVIDER}_key
            ON public.{load_table} USING btree ({col.PROVIDER});
            '''
        )
    )
    postgres.run(
        dedent(
            f'''
            CREATE INDEX IF NOT EXISTS {load_table}_{col.FOREIGN_ID}_key
            ON public.{load_table}
            USING btree (provider, md5(({col.FOREIGN_ID})::text));
            '''
        )
    )
    postgres.run(
        dedent(
            f'''
            CREATE INDEX IF NOT EXISTS {load_table}_{col.DIRECT_URL}_key
            ON public.{load_table}
            USING btree (provider, md5(({col.DIRECT_URL})::text));
            '''
        )
    )


def load_local_data_to_intermediate_table(
        postgres_conn_id,
        tsv_file_name,
        identifier,
        max_rows_to_skip=10
):
    load_table = _get_load_table_name(identifier)
    logger.info(f'Loading {tsv_file_name} into {load_table}')

    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    load_successful = False

    while not load_successful and max_rows_to_skip >= 0:
        try:
            postgres.bulk_load(f'{load_table}', tsv_file_name)
            load_successful = True

        except InvalidTextRepresentation as e:
            line_number = _get_malformed_row_in_file(str(e))
            _delete_malformed_row_in_file(tsv_file_name, line_number)

        finally:
            max_rows_to_skip = max_rows_to_skip - 1

    if not load_successful:
        raise InvalidTextRepresentation(
            'Exceeded the maximum number of allowed defective rows')

    _clean_intermediate_table_data(postgres, load_table)


def load_s3_data_to_intermediate_table(
        postgres_conn_id,
        bucket,
        s3_key,
        identifier
):
    load_table = _get_load_table_name(identifier)
    logger.info(f'Loading {s3_key} from S3 Bucket {bucket} into {load_table}')

    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    postgres.run(
        dedent(
            f"""
            SELECT aws_s3.table_import_from_s3(
              '{load_table}',
              '',
              'DELIMITER E''\t''',
              '{bucket}',
              '{s3_key}',
              'us-east-1'
            );
            """
        )
    )
    _clean_intermediate_table_data(postgres, load_table)


def _clean_intermediate_table_data(
        postgres_hook,
        load_table
):
    postgres_hook.run(
        f'DELETE FROM {load_table} WHERE {col.DIRECT_URL} IS NULL;'
    )
    postgres_hook.run(
        f'DELETE FROM {load_table} WHERE {col.LICENSE} IS NULL;'
    )
    postgres_hook.run(
        f'DELETE FROM {load_table} WHERE {col.LANDING_URL} IS NULL;'
    )
    postgres_hook.run(
        f'DELETE FROM {load_table} WHERE {col.FOREIGN_ID} IS NULL;'
    )
    postgres_hook.run(
        dedent(
            f'''
            DELETE FROM {load_table} p1
            USING {load_table} p2
            WHERE
              p1.ctid < p2.ctid
              AND p1.{col.PROVIDER} = p2.{col.PROVIDER}
              AND p1.{col.FOREIGN_ID} = p2.{col.FOREIGN_ID};
            '''
        )
    )


def upsert_records_to_image_table(
        postgres_conn_id,
        identifier,
        image_table=IMAGE_TABLE_NAME
):

    def _newest_non_null(column):
        return f'{column} = COALESCE(EXCLUDED.{column}, old.{column})'

    def _merge_jsonb_objects(column):
        """
        This function returns SQL that merges the top-level keys of the
        a JSONB column, taking the newest available non-null value.
        """
        return f'''{column} = COALESCE(
            jsonb_strip_nulls(old.{column})
              || jsonb_strip_nulls(EXCLUDED.{column}),
            EXCLUDED.{column},
            old.{column}
          )'''

    def _merge_jsonb_arrays(column):
        return f'''{column} = COALESCE(
            (
              SELECT jsonb_agg(DISTINCT x)
              FROM jsonb_array_elements(old.{column} || EXCLUDED.{column}) t(x)
            ),
            EXCLUDED.{column},
            old.{column}
          )'''

    load_table = _get_load_table_name(identifier)
    logger.info(f'Upserting new records into {image_table}.')
    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    column_inserts = {
        col.CREATED_ON: NOW,
        col.UPDATED_ON: NOW,
        col.INGESTION_TYPE: col.INGESTION_TYPE,
        col.PROVIDER: col.PROVIDER,
        col.SOURCE: col.SOURCE,
        col.FOREIGN_ID: col.FOREIGN_ID,
        col.LANDING_URL: col.LANDING_URL,
        col.DIRECT_URL: col.DIRECT_URL,
        col.THUMBNAIL: col.THUMBNAIL,
        col.WIDTH: col.WIDTH,
        col.HEIGHT: col.HEIGHT,
        col.FILESIZE: col.FILESIZE,
        col.LICENSE: col.LICENSE,
        col.LICENSE_VERSION: col.LICENSE_VERSION,
        col.CREATOR: col.CREATOR,
        col.CREATOR_URL: col.CREATOR_URL,
        col.TITLE: col.TITLE,
        col.LAST_SYNCED: NOW,
        col.REMOVED: FALSE,
        col.META_DATA: col.META_DATA,
        col.TAGS: col.TAGS,
        col.WATERMARKED: col.WATERMARKED
    }
    upsert_query = dedent(
        f'''
        INSERT INTO {image_table} AS old ({', '.join(column_inserts.keys())})
        SELECT {', '.join(column_inserts.values())}
        FROM {load_table}
        ON CONFLICT ({col.PROVIDER}, md5({col.FOREIGN_ID}))
        DO UPDATE SET
          {col.UPDATED_ON} = {NOW},
          {col.LAST_SYNCED} = {NOW},
          {col.REMOVED} = {FALSE},
          {_newest_non_null(col.INGESTION_TYPE)},
          {_newest_non_null(col.SOURCE)},
          {_newest_non_null(col.LANDING_URL)},
          {_newest_non_null(col.DIRECT_URL)},
          {_newest_non_null(col.THUMBNAIL)},
          {_newest_non_null(col.WIDTH)},
          {_newest_non_null(col.HEIGHT)},
          {_newest_non_null(col.FILESIZE)},
          {_newest_non_null(col.LICENSE)},
          {_newest_non_null(col.LICENSE_VERSION)},
          {_newest_non_null(col.CREATOR)},
          {_newest_non_null(col.CREATOR_URL)},
          {_newest_non_null(col.TITLE)},
          {_newest_non_null(col.WATERMARKED)},
          {_merge_jsonb_objects(col.META_DATA)},
          {_merge_jsonb_arrays(col.TAGS)}
        '''
    )
    postgres.run(upsert_query)


def drop_load_table(postgres_conn_id, identifier):
    load_table = _get_load_table_name(identifier)
    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    postgres.run(f'DROP TABLE {load_table};')


def _get_load_table_name(
        identifier,
        load_table_name_stub=LOAD_TABLE_NAME_STUB,
):
    return f'{load_table_name_stub}{identifier}'


def _get_malformed_row_in_file(error_msg):
    error_list = error_msg.splitlines()
    copy_error = next(
        (line for line in error_list if line.startswith('COPY')), None
    )
    assert copy_error is not None

    line_number = int(copy_error.split('line ')[1].split(',')[0])

    return line_number


def _delete_malformed_row_in_file(tsv_file_name, line_number):
    with open(tsv_file_name, "r") as read_obj:
        lines = read_obj.readlines()

    with open(tsv_file_name, "w") as write_obj:
        for index, line in enumerate(lines):
            if index + 1 != line_number:
                write_obj.write(line)


def _create_temp_flickr_sub_prov_table(
        postgres_conn_id,
        temp_table='temp_flickr_sub_prov_table'
):
    """
    Drop the temporary table if it already exists
    """
    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    postgres.run(f'DROP TABLE IF EXISTS public.{temp_table};')

    """
    Create intermediary table for sub provider migration
    """
    postgres.run(
        dedent(
            f'''
            CREATE TABLE public.{temp_table} (
              {col.CREATOR_URL} character varying(2000),
              sub_provider character varying(80)
            );
            '''
        )
    )

    postgres.run(
        f'ALTER TABLE public.{temp_table} OWNER TO {DB_USER_NAME};'
    )

    """
    Populate the intermediary table with the sub providers of interest
    """
    for sub_prov, user_id_set in prov.FLICKR_SUB_PROVIDERS.items():
        for user_id in user_id_set:
            creator_url = prov.FLICKR_PHOTO_URL_BASE + user_id
            postgres.run(
                dedent(
                    f'''
                    INSERT INTO public.{temp_table} (
                      {col.CREATOR_URL},
                      sub_provider
                    )
                    VALUES (
                      '{creator_url}',
                      '{sub_prov}'
                    );
                    '''
                )
            )

    return temp_table


def update_flickr_sub_providers(
  postgres_conn_id,
  image_table=IMAGE_TABLE_NAME,
  default_provider=prov.FLICKR_DEFAULT_PROVIDER,
):
    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    temp_table = _create_temp_flickr_sub_prov_table(postgres_conn_id)

    select_query = dedent(
        f'''
        SELECT
        {col.FOREIGN_ID} AS foreign_id,
        public.{temp_table}.sub_provider AS sub_provider
        FROM {image_table}
        INNER JOIN public.{temp_table}
        ON
        {image_table}.{col.CREATOR_URL} = public.{temp_table}.{
        col.CREATOR_URL}
        AND
        {image_table}.{col.PROVIDER} = '{default_provider}';
        '''
    )

    selected_records = postgres.get_records(select_query)
    logger.info(f'Updating {len(selected_records)} records')

    for row in selected_records:
        foreign_id = row[0]
        sub_provider = row[1]
        postgres.run(
            dedent(
                f'''
                UPDATE {image_table}
                SET {col.SOURCE} = '{sub_provider}'
                WHERE
                {image_table}.{col.PROVIDER} = '{default_provider}'
                AND
                MD5({image_table}.{col.FOREIGN_ID}) = MD5('{foreign_id}');
                '''
            )
        )

    """
    Drop the temporary table
    """
    postgres.run(f'DROP TABLE public.{temp_table};')


def _create_temp_europeana_sub_prov_table(
        postgres_conn_id,
        temp_table='temp_eur_sub_prov_table'
):
    """
    Drop the temporary table if it already exists
    """
    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    postgres.run(f'DROP TABLE IF EXISTS public.{temp_table};')

    """
    Create intermediary table for sub provider migration
    """
    postgres.run(
        dedent(
            f'''
            CREATE TABLE public.{temp_table} (
              data_provider character varying(120),
              sub_provider character varying(80)
            );
            '''
        )
    )

    postgres.run(
        f'ALTER TABLE public.{temp_table} OWNER TO {DB_USER_NAME};'
    )

    """
    Populate the intermediary table with the sub providers of interest
    """
    for sub_prov, data_provider in prov.EUROPEANA_SUB_PROVIDERS.items():
        postgres.run(
            dedent(
                f'''
                INSERT INTO public.{temp_table} (
                  data_provider,
                  sub_provider
                )
                VALUES (
                  '{data_provider}',
                  '{sub_prov}'
                );
                '''
            )
        )

    return temp_table


def update_europeana_sub_providers(
  postgres_conn_id,
  image_table=IMAGE_TABLE_NAME,
  default_provider=prov.EUROPEANA_DEFAULT_PROVIDER,
  sub_providers=prov.EUROPEANA_SUB_PROVIDERS
):
    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)
    temp_table = _create_temp_europeana_sub_prov_table(postgres_conn_id)

    select_query = dedent(
        f'''
        SELECT L.foreign_id, L.data_providers, R.sub_provider
        FROM(
        SELECT
        {col.FOREIGN_ID} AS foreign_id,
        {col.META_DATA} ->> 'dataProvider' AS data_providers,
        {col.META_DATA}
        FROM {image_table}
        WHERE {col.PROVIDER} = '{default_provider}'
        ) L INNER JOIN
        {temp_table} R ON
        L.{col.META_DATA} ->'dataProvider' ? R.data_provider;
        '''
    )

    selected_records = postgres.get_records(select_query)

    """
    Update each selected row if it corresponds to only one sub-provider.
    Otherwise an exception is thrown
    """
    for row in selected_records:
        foreign_id = row[0]
        data_providers = json.loads(row[1])
        sub_provider = row[2]

        eligible_sub_providers = {s for s in sub_providers if sub_providers[s]
                                  in data_providers}
        if len(eligible_sub_providers) > 1:
            raise Exception(f"More than one sub-provider identified for the "
                            f"image with foreign ID {foreign_id}")

        assert len(eligible_sub_providers) == 1
        assert eligible_sub_providers.pop() == sub_provider

        postgres.run(
            dedent(
                f'''
                UPDATE {image_table}
                SET {col.SOURCE} = '{sub_provider}'
                WHERE
                {image_table}.{col.PROVIDER} = '{default_provider}'
                AND
                MD5({image_table}.{col.FOREIGN_ID}) = MD5('{foreign_id}');
                '''
            )
        )

    """
    Drop the temporary table
    """
    postgres.run(f'DROP TABLE public.{temp_table};')


def update_smithsonian_sub_providers(
  postgres_conn_id,
  image_table=IMAGE_TABLE_NAME,
  default_provider=prov.SMITHSONIAN_DEFAULT_PROVIDER,
  sub_providers=prov.SMITHSONIAN_SUB_PROVIDERS
):
    postgres = PostgresHook(postgres_conn_id=postgres_conn_id)

    """
    Select all records where the source value is not yet updated
    """
    select_query = dedent(
        f'''
        SELECT {col.FOREIGN_ID},
        {col.META_DATA} ->> 'unit_code' AS unit_code
        FROM {image_table}
        WHERE
        {col.PROVIDER} = '{default_provider}'
        AND
        {col.SOURCE} = '{default_provider}';
        '''
    )

    selected_records = postgres.get_records(select_query)

    """
    Set the source value of each selected row to the sub-provider value
    corresponding to unit code. If the unit code is unknown, an error is thrown
    """
    for row in selected_records:
        foreign_id = row[0]
        unit_code = row[1]

        source = next((s for s in sub_providers if unit_code in
                       sub_providers[s]), None)
        if source is None:
            raise Exception(
                f"An unknown unit code value {unit_code} encountered ")

        postgres.run(
            dedent(
                f'''
                UPDATE {image_table}
                SET {col.SOURCE} = '{source}'
                WHERE
                {image_table}.{col.PROVIDER} = '{default_provider}'
                AND
                MD5({image_table}.{col.FOREIGN_ID}) = MD5('{foreign_id}');
                '''
            )
        )
