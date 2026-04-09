from typing import List, Optional

def calculate_moving_average(data: List[float], window_size: int) -> Optional[float]:
    """
    Calculates the moving average for a given list of numerical data.
    Used for consumption profiling.
    """
    if not data:
        return None
    return sum(data[-window_size:]) / min(len(data), window_size)

def calculate_forecast_error(actual: float, forecast: float) -> float:
    """
    Calculates the absolute and relative error of solar forecast.
    """
    if forecast == 0:
        return 0.0 if actual == 0 else 1.0
    return abs(actual - forecast) / forecast

def calculate_survival_soc(nightly_consumption_mean: float, hours_until_sunrise: float, battery_capacity_kwh: float, occupancy_factor: float = 1.0) -> float:
    """
    Calculates the target SOC needed to survive until sunrise.
    
    :param nightly_consumption_mean: Average kWh consumed per hour at night.
    :param hours_until_sunrise: Time remaining until solar production begins.
    :param battery_capacity_kwh: Total battery capacity.
    :param occupancy_factor: Adjustment based on number of people in the house.
    :return: Target SOC percentage (0-100).
    """
    needed_kwh = nightly_consumption_mean * hours_until_sunrise * occupancy_factor
    target_soc = (needed_kwh / battery_capacity_kwh) * 100
    return min(max(target_soc, 10.0), 100.0)  # Always keep at least 10%
