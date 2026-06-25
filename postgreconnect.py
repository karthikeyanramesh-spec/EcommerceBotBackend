from sqlalchemy import create_engine, text
from dotenv import load_dotenv
import os
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL, echo=True)

try:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        print("PostgreSQL connection successful!")

except Exception as e:
    print("Connection failed")
    print(e)