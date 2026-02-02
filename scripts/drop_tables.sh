#!/bin/bash
   docker exec -it geospatial_db psql -U user -d active_learning -c "
   DROP TABLE IF EXISTS labels;
   DROP TABLE IF EXISTS tiles;
   DROP TABLE IF EXISTS sessions;
   "