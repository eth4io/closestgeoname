#!/usr/bin/python3

import pandas as pd
import sqlite3
import os
import argparse
import urllib.request
import time
import sys
from zipfile import ZipFile
from shapely.geometry import Point

# Schema of geonames databases from
# as of 07/01/2020
COLNAMES = ['Geonameid',
            'Name',
            'Asciiname',
            'Alternatenames',
            'Latitude',
            'Longitude',
            'FeatureClass',
            'FeatureCode',
            'CountryCode',
            'Cc2',
            'Admin1Code',
            'Admin2Code',
            'Admin3Code',
            'Admin4Code',
            'Population',
            'Elevation',
            'Dem',
            'Timezone',
            'ModificationDate']
DBFILENAME = 'geonames.sqlite'
MIN_QUERY_DIST = 0.000001 # Metres

def import_dump(filename, colnames, encoding='utf-8', delimiter='\t'):
    MULTIPLIER = 1.4 # DB size versus original text file
    filesize = os.stat(filename).st_size/1048576 # In MB
    print("Initial filesize\t{} MB\nExpected database size\t{} MB\n".format(
                            round(filesize,2), round(filesize*MULTIPLIER,2)))

    df = pd.read_csv(filename, delimiter=delimiter, encoding=encoding,
                     header=None, names=colnames, low_memory=False)

    # Filter only the necessary columns
    return df[['Geonameid', 'Name', 'Latitude', 'Longitude', 'CountryCode']]

def query_db_size(db_path):
    print("Database size {} MB\n".format(round(os.stat(db_path).st_size/1048576, 2)))

def generate_db(db_path, dataframe):
    with sqlite3.connect(db_path) as conn:
        # Use pandas SQL export to generate SQLite DB
        print("Populating database", end='... ')
        dataframe.to_sql('cities', conn, if_exists='replace', index=False)
        print("Done")
        query_db_size(db_path)

        # Initialise spatialite
        conn.enable_load_extension(True)
        conn.load_extension("mod_spatialite")
        conn.execute("SELECT InitSpatialMetaData(1);")

        # Build geometry columns
        print("Building geometry columns", end='... ')
        conn.execute(
            """
            SELECT AddGeometryColumn('cities', 'geom', 4326, 'POINT', 2);
            """
        )
        print("Done")
        query_db_size(db_path)

        # Form geometry column 'geom' from latitude and logitude columns
        print("Generating spatial columns from lat/long columns", end='... ')
        conn.execute(
            """
            UPDATE cities
            SET geom = MakePoint(Longitude, Latitude, 4326);
            """
        )
        print("Done")
        query_db_size(db_path)

        # Generate the spatial index for super-fast queries
        print("Building spatial index", end='... ')
        conn.execute(
            """
            SELECT createspatialindex('cities', 'geom');
            """
        )
        print("Done")
        query_db_size(db_path)

def query_closest_city(db_path, latitude, longitude, epsg=4326, query_buffer_distance=0.00001):
    # Start the buffer size for searching nearest points at a low number for speed,
    # but keep iterating (doubling distance in size) until somewhere is found.
    # Hence, this is faster for huge datasets as less points are considered in the spatial
    # query, but slower for small datasets as more iterations will need to occur if no point
    # exists
    row = None
    while row is None:
        # Prevent an infinite loop
        if query_buffer_distance > 12756 * 1000: # 12,756 km Longest distance on earth
            sys.exit("Distance more than length of earth. Never going to find this point")

        # Form tuple to represent values missing in SQL query
        query_tuple = (longitude,
                    latitude,
                    epsg,
                    longitude,
                    latitude,
                    epsg,
                    query_buffer_distance)
        with sqlite3.connect(db_path) as conn:
            conn.enable_load_extension(True)
            conn.load_extension("mod_spatialite")
            cur = conn.cursor()

            # Query database with spatial index
            cur.execute(
                """
                select Name, CountryCode from (
                    select *, distance(geom, makepoint(?, ?, ?)) dist
                    from cities
                    where rowid in (
                        select rowid
                        from spatialindex
                        where f_table_name = 'cities'
                        and f_geometry_column = 'geom'
                        and search_frame = buffer(makepoint(?, ?, ?), ?)
                    )
                    order by dist
                    limit 1
                );
                """, query_tuple
            )
            row = cur.fetchone()

        query_buffer_distance *= 2
    return row

# Reporthook function to show file download progress. Code from
# https://blog.shichao.io/2012/10/04/progress_speed_indicator_for_urlretrieve_in_python.html
def reporthook(count, block_size, total_size):
    global start_time
    if count == 0:
        start_time = time.time()
        return
    duration = time.time() - start_time
    progress_size = int(count * block_size)
    speed = int(progress_size / (1024 * duration))
    percent = int(count * block_size * 100 / total_size)
    sys.stdout.write("\r...%d%%, %d MB, %d KB/s, %d seconds passed" %
                    (percent, progress_size / (1024 * 1024), speed, duration))
    sys.stdout.flush()

def download_dataset(colnames, db_path):
    options = [
    ("http://download.geonames.org/export/dump/allCountries.zip",
     "all countries combined in one file (HUGE! > 1.2 GB)"),
    ("http://download.geonames.org/export/dump/cities500.zip",
     "all cities with a population > 500 or seats of adm div down to PPLA4 (ca 185.000)"),
    ("http://download.geonames.org/export/dump/cities1000.zip",
     "all cities with a population > 1000 or seats of adm div down to PPLA3 (ca 130.000)"),
    ("http://download.geonames.org/export/dump/cities5000.zip",
     "all cities with a population > 5000 or PPLA (ca 50.000)"),
    ("http://download.geonames.org/export/dump/cities15000.zip",
     "all cities with a population > 15000 or capitals (ca 25.000)"),
    ]

    # Let user choose which file to download
    for id, option in enumerate(options):
        print("[{}] {}:\t{}".format(id, option[0].split('/')[-1], option[1]))
    choice = int(input("Choose which file to download: "))
    urllib.request.urlretrieve(options[choice][0], "rawdata.zip", reporthook)
    extracted_txt = extract_zip('rawdata.zip')

    print()
    # Create database
    df = import_dump(extracted_txt, colnames)
    generate_db(db_path, df)

    # Remove downloaded files
    os.remove("rawdata.zip")
    os.remove(extracted_txt)
    print("Success")

def extract_zip(filename):
    with ZipFile('rawdata.zip', 'r') as zipObj:
        files = zipObj.namelist()
        # Iterate over the file names
        for fileName in files:
            # Check filename endswith csv
            if fileName.endswith('.txt'):
                # Extract a single file from zip
                zipObj.extract(fileName)
                filename = fileName
    return filename

def main():
    if os.path.exists(os.path.join(os.getcwd(), DBFILENAME)):
        parser = argparse.ArgumentParser()
        parser.add_argument("--database", type=str, help="Set the file for database (default: geonames.sqlite)", default="geonames.sqlite")
        parser.add_argument("longitude", type=float, help="X coordinate (Longitude)")
        parser.add_argument("latitude", type=float, help="Y coordinate (Latitude)")
        args = parser.parse_args()

        result = query_closest_city(args.database, args.latitude, args.longitude,
                                        query_buffer_distance=MIN_QUERY_DIST)
        print("{}, {}".format(result[0], result[1]))
    else:
        print("GeoNames database", DBFILENAME, "does not exist. Choose an option")
        download_dataset(COLNAMES, DBFILENAME)

if __name__ == "__main__":
    main()
