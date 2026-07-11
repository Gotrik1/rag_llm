# Выгрузка по дням и nm_id

### 1) Создание таблицы с данными

Создание таблиц, куда будут записаны необходимые данные на каждом сервере с 1 по 14

```sql
CREATE TABLE IF NOT EXISTS support_schema.wb_manual_analytics_by_days_nm_ids_all_clients (
    client_id BIGINT NOT NULL
    , sid UUID
    , spreadsheet_id VARCHAR(255)
    , date DATE NOT NULL
    , nm_id BIGINT NOT NULL
    , subject_name VARCHAR NOT NULL
    , orders_sum_rub NUMERIC
    , orders_count NUMERIC
    , orders_buyouts_sum_rub_fact NUMERIC
    , orders_buyouts_count_fact NUMERIC
    , adv_percent NUMERIC
    , adv_sum NUMERIC
    , marg_without_adv NUMERIC
    , marg_with_adv NUMERIC
    , profit_without_adv NUMERIC
    , profit_with_adv NUMERIC
)
;
ALTER TABLE support_schema.wb_manual_analytics_by_days_nm_ids_all_clients ADD CONSTRAINT wb_manual_analytics_by_days_nm_ids_all_clients_pkey PRIMARY KEY (date, nm_id)
;
```

### 2) Запуск сбора аналитики

Запускать в DBeaver для серверов с 1 по 14. Можно запускать параллельно.
Не обрабатываются клиенты, у которых больше 5000 артикулов.

```sql
SET statement_timeout = '45min'
;

-- ФУНКЦИЯ ЗАПУСКА
DO $$
DECLARE
    current_schema_name text;
    final_sql text;
	has_all_tables boolean;
    nm_id_count integer;
    sql text;
BEGIN
	RAISE NOTICE '                            ';
	RAISE NOTICE 'new func start';
	RAISE NOTICE '                            ';

    FOR current_schema_name IN 
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name ~ '^schema_\d+$'
        ORDER BY CAST(substring(schema_name FROM '\d+$') AS INTEGER)
        LIMIT 1000
        OFFSET 0
    LOOP
	BEGIN

        sql := format('SELECT COUNT(DISTINCT nm_id) FROM %I.wb_cards', current_schema_name);
        EXECUTE sql INTO nm_id_count;

        IF nm_id_count > 5000 THEN
            RAISE NOTICE 'Schema % skipped: more than 5000 distinct nm_id in wb_cards', current_schema_name;
            CONTINUE;
        END IF;

       SELECT bool_and(exists_flag) INTO has_all_tables
        FROM (
            VALUES 
                ('wb_cards'),
                ('ds_wb10x_checklist_by_days_and_nm_ids_v1'),
                ('wb_cards_history')
        ) t(tbl)
        LEFT JOIN LATERAL (
            SELECT EXISTS (
                SELECT 1 
                FROM information_schema.tables 
                WHERE table_schema = current_schema_name 
                  AND table_name = t.tbl
            ) AS exists_flag
        ) chk ON true;

        IF NOT has_all_tables THEN
            RAISE NOTICE 'Skipping schema % because not all required tables exist', current_schema_name;
            CONTINUE;
        END IF;

        final_sql := format($f$


            WITH
                nm_ids_subject_names AS (
                    SELECT
                        nm_id
                        , COALESCE(subject_name, '-') AS subject_name
                        , sid
                    FROM %I.wb_cards w1
                    WHERE updated_at = (
                        SELECT MAX(updated_at)
                        FROM %I.wb_cards w2
                        WHERE w2.nm_id = w1.nm_id
                    )
                )
                , nm_ids AS (
                    SELECT
                        nm_id
                        , COALESCE(subject_name, '-') AS subject_name
                    FROM %I.wb_cards w1
                    WHERE updated_at = (
                        SELECT MAX(updated_at)
                        FROM %I.wb_cards w2
                        WHERE w2.nm_id = w1.nm_id
                    )
                )
                , spreadsheet_ids AS (
                    select
                        ROW_NUMBER() OVER (PARTITION BY client_id ORDER BY updated_at DESC) AS rn
                        , spreadsheet_id
                    from public.spreadsheets
                    where template_name = 'wb10xMain'
                    and client_id = SUBSTRING(%L, 8)::BIGINT
                    order by updated_at desc
                )
                , data_to_analyse_by_days_and_nm_ids AS (
                    SELECT
                        SUBSTRING(%L, 8)::BIGINT AS client_id
                        , COALESCE(wch.sid, nmsn.sid) AS sid
                        , sprsh.spreadsheet_id
                        , d.date
                        , nm.nm_id
                        , nmsn.subject_name
                        , COALESCE(d.orders_sum_rub, 0) AS orders_sum_rub
                        , COALESCE(d.orders_count, 0) AS orders_count
                        , COALESCE(d.orders_buyouts_sum_rub_fact, 0) AS orders_buyouts_sum_rub_fact
                        , COALESCE(d.orders_buyouts_count_fact, 0) AS orders_buyouts_count_fact
                        , COALESCE(d.adv_percent, 0) AS adv_percent
                        , COALESCE(d.adv_sum, 0) AS adv_sum
                        , COALESCE(d.marg_without_adv, 0) AS marg_without_adv
                        , COALESCE(d.marg_with_adv, 0) AS marg_with_adv
                        , COALESCE(d.profit_without_adv, 0) AS profit_without_adv
                        , COALESCE(d.profit_with_adv, 0) AS profit_with_adv
                    FROM
                        nm_ids nm
                        LEFT JOIN %I.ds_wb10x_checklist_by_days_and_nm_ids_v1 d ON d.nm_id = nm.nm_id AND d.date >= CURRENT_DATE - 365
                        LEFT JOIN nm_ids_subject_names nmsn ON nmsn.nm_id = nm.nm_id
                        LEFT JOIN spreadsheet_ids sprsh ON sprsh.rn = 1
                        LEFT JOIN %I.wb_cards_history wch ON wch.date = d.date AND wch.nm_id = d.nm_id
                )
                , result AS (
                    SELECT * FROM data_to_analyse_by_days_and_nm_ids
                    WHERE FALSE
                        OR orders_sum_rub > 0
                        OR orders_count > 0
                        OR orders_buyouts_sum_rub_fact > 0
                        OR orders_buyouts_count_fact > 0
                        OR adv_percent > 0
                        OR adv_sum > 0
                        OR marg_without_adv > 0
                        OR marg_with_adv > 0
                        OR profit_without_adv > 0
                        OR profit_with_adv > 0
                    ORDER BY nm_id, date DESC
                )
                -- SELECT COUNT(*) FROM result
                -- ;
                INSERT INTO support_schema.wb_manual_analytics_by_days_nm_ids_all_clients (
                    client_id
                    , sid
                    , spreadsheet_id
                    , date
                    , nm_id
                    , subject_name
                    , orders_sum_rub
                    , orders_count
                    , orders_buyouts_sum_rub_fact
                    , orders_buyouts_count_fact
                    , adv_percent
                    , adv_sum
                    , marg_without_adv
                    , marg_with_adv
                    , profit_without_adv
                    , profit_with_adv
                )
                SELECT * FROM result
                ON CONFLICT DO NOTHING
                
        $f$, current_schema_name, current_schema_name, current_schema_name, current_schema_name, current_schema_name, current_schema_name, current_schema_name, current_schema_name);
		
        RAISE NOTICE 'Executing for %', current_schema_name;
--		RAISE NOTICE 'SQL = %', final_sql;

        EXECUTE final_sql;
		
	    EXCEPTION
	        WHEN OTHERS THEN
	            RAISE NOTICE 'Skipping schema % due to error: %', current_schema_name, SQLERRM;
	END;
    END LOOP;
END;
$$ LANGUAGE plpgsql;
```

### 3) Сбор информации на втором сервере mp-sl-2

Необходимо скопировать таблицу support_schema.wb_manual_analytics_by_days_nm_ids_all_clients с каждого сервера на второй.
Для каждого запуска нужно изменять название таблицы и "TARGET_TABLE" и параметры соединения

Пример запуска:

```sh
node transfer_data.sh
```

Пример файла "transfer_data.sh":

```sh
#!/bin/bash

# Параметры источника
SOURCE_HOST="192.168.150.32"
SOURCE_PORT="5432"
SOURCE_DB="wb"
SOURCE_USER="<ваш логин>"
SOURCE_PASSWORD="<ваш пароль>"

# Параметры назначения
TARGET_HOST="192.168.150.32"
TARGET_PORT="5432"
TARGET_DB="wb"
TARGET_USER="<ваш логин>"
TARGET_PASSWORD="<ваш пароль>"

# Таблицы и поля
SOURCE_TABLE="support_schema.wb_manual_analytics_by_days_nm_ids_all_clients"
TARGET_TABLE="support_schema.wb_manual_analytics_by_days_nm_ids_all_servers_02"
COLUMNS_TO_INSERT="client_id, sid, spreadsheet_id, date, nm_id, subject_name, orders_sum_rub, orders_count, orders_buyouts_sum_rub_fact, orders_buyouts_count_fact, adv_percent, adv_sum, marg_without_adv, marg_with_adv, profit_without_adv, profit_with_adv"  # укажите нужные поля

# Таймаут выполнения SQL (в миллисекундах): 10 минут
STATEMENT_TIMEOUT_MS="900000"

# Передача данных из источника
PGOPTIONS="-c statement_timeout=$STATEMENT_TIMEOUT_MS" PGPASSWORD=$SOURCE_PASSWORD psql -h "$SOURCE_HOST" -p "$SOURCE_PORT" -U "$SOURCE_USER" -d "$SOURCE_DB" \
 -c "COPY (SELECT $COLUMNS_TO_INSERT FROM $SOURCE_TABLE) TO STDOUT" | \
  
# Вставка сразу на целевой сервер
PGOPTIONS="-c statement_timeout=$STATEMENT_TIMEOUT_MS" PGPASSWORD=$TARGET_PASSWORD psql -h "$TARGET_HOST" -p "$TARGET_PORT" -U "$TARGET_USER" -d "$TARGET_DB" \
 -c "\copy $TARGET_TABLE ($COLUMNS_TO_INSERT) FROM STDIN"
```

### 4) Объединение данных в 2 таблицы

Все таблицы вида "wb_manual_analytics_by_days_nm_ids_all_servers_XX" нужно объединить в одну общую таблицу (или 2 таблицы).
Если размер таблицы превышвает 4 Гб - остальные данные переместить во вторую таблицу.

Вставка данных из одной таблицы в общую:

```sql
INSERT INTO wb_manual_analytics_by_days_nm_ids_all_servers
SELECT * FROM wb_manual_analytics_by_days_nm_ids_all_servers_01
ON CONFLICT DO NOTHING
;
```

Нужно повторить от 1 до 7 сервера.
Данные с 8 по 14 сервер - добавить в новую таблицу wb_manual_analytics_by_days_nm_ids_all_servers_2

### 5) Пример запроса к обеим таблицам с данными всех серверов

```sql
WITH
    dates_filter AS (
        SELECT 1
            , '2026-02-01'::DATE AS date_from
            , '2026-02-28'::DATE AS date_to
    )
    , b AS (
        SELECT
            generate_series((SELECT date_from FROM dates_filter), (SELECT date_to FROM dates_filter), '1 day'::interval)::DATE AS date
    )
    , data_1 AS (
        SELECT
            d.sid
            , SUM(d.orders_sum_rub) AS orders_sum_rub
        FROM
            b
            LEFT JOIN wb_manual_analytics_by_days_nm_ids_all_servers d USING (date)
        GROUP BY (d.sid)
    )
    , data_2 AS (
        SELECT
            d.sid
            , SUM(d.orders_sum_rub) AS orders_sum_rub
        FROM
            b
            LEFT JOIN wb_manual_analytics_by_days_nm_ids_all_servers_2 d USING (date)
        GROUP BY (d.sid)
    )
    , data_all AS (
        SELECT * FROM data_1
        UNION ALL
        SELECT * FROM data_2
    )
    , sids_spreadsheet_ids AS (
        SELECT DISTINCT sid, spreadsheet_id from wb_manual_analytics_by_days_nm_ids_all_servers
        UNION ALL
        SELECT DISTINCT sid, spreadsheet_id from wb_manual_analytics_by_days_nm_ids_all_servers_2
    )
    , result AS (
        SELECT
            d.sid
            , s.spreadsheet_id
            , d.orders_sum_rub
        FROM
            data_all d
            LEFT JOIN sids_spreadsheet_ids s USING (sid)
    )
    SELECT * from result
    ;
```