-- Return all tiles and labels (no filter). No params.
SELECT
    t.id   AS tile_pk,
    t.tile_id,
    l.id   AS label_id
FROM sessions s
JOIN tiles t ON t.session_id = s.id
LEFT JOIN labels l ON l.tile_pk = t.id AND l.session_id = t.session_id
ORDER BY t.id, l.id;
