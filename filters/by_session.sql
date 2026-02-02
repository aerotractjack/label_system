-- Virtual dataset for a single session. Pass :session_id (e.g. --session_id 3881fad6-887c-4747-81af-199f167a75aa).
SELECT
    t.id   AS tile_pk,
    t.tile_id,
    l.id   AS label_id
FROM sessions s
JOIN tiles t ON t.session_id = s.id
LEFT JOIN labels l ON l.tile_pk = t.id AND l.session_id = t.session_id
WHERE s.id::text = :session_id
ORDER BY t.id, l.id;
