from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
import datetime

Base = declarative_base()

class SensorHistory(Base):
    __tablename__ = "sensor_history"
    id = Column(Integer, primary_key=True)
    entity_id = Column(String, index=True)
    value = Column(Float)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)

class LoadState(Base):
    __tablename__ = "load_states"
    id = Column(Integer, primary_key=True)
    entity_id = Column(String, unique=True)
    state = Column(String)  # idle, waiting, running
    last_cycle_start = Column(DateTime)
    metadata_json = Column(JSON)  # For storing temperature, energy etc.

class Profile(Base):
    __tablename__ = "profiles"
    id = Column(Integer, primary_key=True)
    name = Column(String)  # consumption, solar
    day_of_week = Column(Integer)  # 0-6
    hour = Column(Integer)  # 0-23
    mean_value = Column(Float)

class SolarHourlyStat(Base):
    __tablename__ = "solar_hourly_stats"
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, index=True)
    hour = Column(Integer)
    actual_kwh = Column(Float)
    forecast_kwh = Column(Float)

class SystemSetting(Base):
    __tablename__ = "system_settings"
    key = Column(String, primary_key=True)
    value = Column(JSON)


# Database setup
import os
db_path = "/data/ems_data.db" if os.path.exists("/data") else "ems_data.db"
engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    Base.metadata.create_all(bind=engine)
