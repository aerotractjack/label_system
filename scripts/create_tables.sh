#!/bin/bash
docker exec -i geospatial_db psql -U user -d active_learning < /home/aerotract/2software/labeler/postgis_schema/schema.sql