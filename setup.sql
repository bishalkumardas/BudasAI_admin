CREATE OR REPLACE FUNCTION public.resequence_daily_news_ids()
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $function$
DECLARE
	article_count bigint;
	max_article_id bigint;
	temporary_offset numeric;
	id_type text;
	sequence_name text;
BEGIN
	LOCK TABLE public.daily_news IN ACCESS EXCLUSIVE MODE;

	SELECT data_type
	INTO id_type
	FROM information_schema.columns
	WHERE table_schema = 'public'
	  AND table_name = 'daily_news'
	  AND column_name = 'id';

	IF id_type NOT IN ('integer', 'bigint') THEN
		RAISE EXCEPTION 'daily_news.id must be an integer or bigint column';
	END IF;

	SELECT count(*), coalesce(max(id)::bigint, 0)
	INTO article_count, max_article_id
	FROM public.daily_news;

	sequence_name := pg_get_serial_sequence('public.daily_news', 'id');

	IF article_count = 0 THEN
		IF sequence_name IS NOT NULL THEN
			PERFORM setval(sequence_name, 1, false);
		END IF;
		RETURN 0;
	END IF;

	temporary_offset := max_article_id::numeric + article_count::numeric;

	IF id_type = 'integer'
	   AND temporary_offset + article_count > 2147483647 THEN
		RAISE EXCEPTION 'Not enough integer ID space to safely reassign news IDs';
	END IF;

	IF id_type = 'bigint'
	   AND temporary_offset + article_count > 9223372036854775807 THEN
		RAISE EXCEPTION 'Not enough bigint ID space to safely reassign news IDs';
	END IF;

	WITH ranked_articles AS (
		SELECT
			id,
			row_number() OVER (
				ORDER BY published_at DESC NULLS LAST, id DESC
			) AS new_rank
		FROM public.daily_news
	)
	UPDATE public.daily_news AS article
	SET id = temporary_offset + ranked_articles.new_rank
	FROM ranked_articles
	WHERE article.id = ranked_articles.id;

	UPDATE public.daily_news
	SET id = id - temporary_offset
	WHERE id > temporary_offset;

	IF sequence_name IS NOT NULL THEN
		PERFORM setval(sequence_name, article_count, true);
	END IF;

	RETURN article_count::integer;
END;
$function$;

REVOKE ALL ON FUNCTION public.resequence_daily_news_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.resequence_daily_news_ids() TO service_role;
