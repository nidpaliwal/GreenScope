#!/usr/bin/env python3
"""
SQLite to PostgreSQL Migration Script for GreenScope

Run this script once to migrate existing SQLite data to PostgreSQL.
Usage: python scripts/migrate_sqlite_to_pg.py

Requires:
- SQLITE_PATH environment variable (or default reports.db)
- DATABASE_URL environment variable for PostgreSQL connection
"""

import os
import sys
import json
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime


def get_sqlite_connection():
    """Get connection to SQLite database."""
    sqlite_path = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "..", "app", "reports.db"))
    if sqlite_path.startswith("sqlite:///"):
        sqlite_path = sqlite_path[len("sqlite:///"):]
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_postgres_connection():
    """Get connection to PostgreSQL database."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL environment variable not set")
    if db_url.startswith("sqlite"):
        raise ValueError("DATABASE_URL must be a PostgreSQL connection string")
    conn = psycopg2.connect(db_url, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def init_postgres_schema(pg_conn):
    """Initialize PostgreSQL schema with all required tables."""
    with pg_conn.cursor() as cur:
        # Reports table with JSONB
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                report_id TEXT PRIMARY KEY,
                location TEXT,
                latitude REAL,
                longitude REAL,
                environment JSONB,
                photo_analysis JSONB,
                recommendations JSONB,
                generated_at REAL,
                processing_time_ms REAL,
                rejected_plants JSONB,
                categories JSONB,
                garden_risk JSONB,
                comparison_table JSONB,
                garden_bed JSONB,
                data_source TEXT
            )
        """)
        # Feedback table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                report_id TEXT,
                plant_name TEXT,
                vote TEXT CHECK(vote IN ('up', 'down')),
                created_at REAL
            )
        """)
        # Purchase tracking table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id SERIAL PRIMARY KEY,
                report_id TEXT,
                plant_name TEXT,
                item_type TEXT CHECK(item_type IN ('seeds', 'saplings', 'fertilizer', 'tools', 'other')),
                item_name TEXT,
                quantity REAL,
                unit TEXT,
                cost_per_unit REAL,
                total_cost REAL,
                purchase_date REAL,
                notes TEXT,
                created_at REAL
            )
        """)
        # Sales tracking table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id SERIAL PRIMARY KEY,
                report_id TEXT,
                plant_name TEXT,
                item_name TEXT,
                quantity REAL,
                unit TEXT,
                price_per_unit REAL,
                total_revenue REAL,
                sale_date REAL,
                notes TEXT,
                created_at REAL
            )
        """)
        # Soil health logs table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS soil_health_logs (
                id SERIAL PRIMARY KEY,
                report_id TEXT REFERENCES reports(report_id),
                plant_name TEXT,
                log_date TIMESTAMP DEFAULT NOW(),
                ph REAL,
                organic_matter_pct REAL,
                nitrogen_ppm REAL,
                phosphorus_ppm REAL,
                potassium_ppm REAL,
                notes TEXT
            )
        """)
        # Plant health logs table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS plant_health_logs (
                id SERIAL PRIMARY KEY,
                report_id TEXT REFERENCES reports(report_id),
                plant_name TEXT,
                log_date TIMESTAMP DEFAULT NOW(),
                symptoms JSONB,
                diagnosis TEXT,
                severity TEXT,
                treatment TEXT,
                photo_url TEXT
            )
        """)
    pg_conn.commit()
    print("PostgreSQL schema initialized successfully")


def migrate_reports(sqlite_conn, pg_conn):
    """Migrate reports from SQLite to PostgreSQL."""
    print("Migrating reports...")
    sqlite_rows = sqlite_conn.execute("SELECT * FROM reports").fetchall()
    
    with pg_conn.cursor() as cur:
        for row in sqlite_rows:
            # Convert SQLite row to dict
            data = dict(row)
            
            # Parse JSON fields
            json_fields = ['environment', 'photo_analysis', 'recommendations', 
                          'rejected_plants', 'categories', 'garden_risk', 
                          'comparison_table', 'garden_bed']
            for field in json_fields:
                if data.get(field):
                    try:
                        data[field] = json.dumps(json.loads(data[field]))
                    except (json.JSONDecodeError, TypeError):
                        data[field] = 'null'
                else:
                    data[field] = 'null'
            
            cur.execute("""
                INSERT INTO reports 
                (report_id, location, latitude, longitude, environment, photo_analysis, 
                 recommendations, generated_at, processing_time_ms, rejected_plants, 
                 categories, garden_risk, comparison_table, garden_bed, data_source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (report_id) DO UPDATE SET
                    location = EXCLUDED.location,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    environment = EXCLUDED.environment,
                    photo_analysis = EXCLUDED.photo_analysis,
                    recommendations = EXCLUDED.recommendations,
                    generated_at = EXCLUDED.generated_at,
                    processing_time_ms = EXCLUDED.processing_time_ms,
                    rejected_plants = EXCLUDED.rejected_plants,
                    categories = EXCLUDED.categories,
                    garden_risk = EXCLUDED.garden_risk,
                    comparison_table = EXCLUDED.comparison_table,
                    garden_bed = EXCLUDED.garden_bed,
                    data_source = EXCLUDED.data_source
            """, (
                data['report_id'], data['location'], data['latitude'], data['longitude'],
                data['environment'], data['photo_analysis'], data['recommendations'],
                data['generated_at'], data['processing_time_ms'], data['rejected_plants'],
                data['categories'], data['garden_risk'], data['comparison_table'], 
                data['garden_bed'], data['data_source']
            ))
    pg_conn.commit()
    print(f"Migrated {len(sqlite_rows)} reports")


def migrate_feedback(sqlite_conn, pg_conn):
    """Migrate feedback from SQLite to PostgreSQL."""
    print("Migrating feedback...")
    sqlite_rows = sqlite_conn.execute("SELECT * FROM feedback").fetchall()
    
    with pg_conn.cursor() as cur:
        for row in sqlite_rows:
            data = dict(row)
            cur.execute("""
                INSERT INTO feedback (report_id, plant_name, vote, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (data['report_id'], data['plant_name'], data['vote'], data['created_at']))
    pg_conn.commit()
    print(f"Migrated {len(sqlite_rows)} feedback entries")


def migrate_purchases(sqlite_conn, pg_conn):
    """Migrate purchases from SQLite to PostgreSQL."""
    print("Migrating purchases...")
    sqlite_rows = sqlite_conn.execute("SELECT * FROM purchases").fetchall()
    
    with pg_conn.cursor() as cur:
        for row in sqlite_rows:
            data = dict(row)
            cur.execute("""
                INSERT INTO purchases 
                (report_id, plant_name, item_type, item_name, quantity, unit, 
                 cost_per_unit, total_cost, purchase_date, notes, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (
                data['report_id'], data['plant_name'], data['item_type'], 
                data['item_name'], data['quantity'], data['unit'],
                data['cost_per_unit'], data['total_cost'], data['purchase_date'],
                data['notes'], data['created_at']
            ))
    pg_conn.commit()
    print(f"Migrated {len(sqlite_rows)} purchase entries")


def migrate_sales(sqlite_conn, pg_conn):
    """Migrate sales from SQLite to PostgreSQL."""
    print("Migrating sales...")
    sqlite_rows = sqlite_conn.execute("SELECT * FROM sales").fetchall()
    
    with pg_conn.cursor() as cur:
        for row in sqlite_rows:
            data = dict(row)
            cur.execute("""
                INSERT INTO sales 
                (report_id, plant_name, item_name, quantity, unit, price_per_unit, 
                 total_revenue, sale_date, notes, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, (
                data['report_id'], data['plant_name'], data['item_name'],
                data['quantity'], data['unit'], data['price_per_unit'],
                data['total_revenue'], data['sale_date'], data['notes'], data['created_at']
            ))
    pg_conn.commit()
    print(f"Migrated {len(sqlite_rows)} sale entries")


def main():
    print("=" * 60)
    print("GreenScope SQLite to PostgreSQL Migration")
    print("=" * 60)
    
    # Check environment variables
    if not os.environ.get("DATABASE_URL"):
        print("ERROR: DATABASE_URL environment variable not set")
        print("Set it to your PostgreSQL connection string:")
        print("  export DATABASE_URL='postgresql://user:pass@host:port/dbname'")
        sys.exit(1)
    
    # Get connections
    try:
        sqlite_conn = get_sqlite_connection()
        print(f"Connected to SQLite")
    except Exception as e:
        print(f"ERROR connecting to SQLite: {e}")
        sys.exit(1)
    
    try:
        pg_conn = get_postgres_connection()
        print(f"Connected to PostgreSQL")
    except Exception as e:
        print(f"ERROR connecting to PostgreSQL: {e}")
        sys.exit(1)
    
    try:
        # Initialize PostgreSQL schema
        init_postgres_schema(pg_conn)
        
        # Migrate all tables
        migrate_reports(sqlite_conn, pg_conn)
        migrate_feedback(sqlite_conn, pg_conn)
        migrate_purchases(sqlite_conn, pg_conn)
        migrate_sales(sqlite_conn, pg_conn)
        
        print("=" * 60)
        print("Migration completed successfully!")
        print("=" * 60)
        
    except Exception as e:
        print(f"ERROR during migration: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()