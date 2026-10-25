"""Create the HW5 tables in an existing PostgreSQL database; retain all rows."""

from service1.database import close_database, create_schema


if __name__ == "__main__":
    try:
        create_schema()
        print("HW5 schema ready; existing data preserved.")
    finally:
        close_database()
