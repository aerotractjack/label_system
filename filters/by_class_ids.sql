-- Virtual dataset for labels with given class_ids only. Pass :class_ids as array (e.g. --class_ids 0 1 2).
-- Tiles appear if they have at least one label in those classes; label_ids list only includes labels in those classes.
SELECT
    t.id   AS tile_pk,
    t.tile_id,
    l.id   AS label_id
FROM sessions s
JOIN tiles t ON t.session_id = s.id
JOIN labels l ON l.tile_pk = t.id AND l.session_id = t.session_id
WHERE l.class_id = ANY(:class_ids)
ORDER BY t.id, l.id;
