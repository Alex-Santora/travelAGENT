from math import asin, cos, radians, sin, sqrt


def distance_km(a, b):
    lat1, lat2 = radians(a["latitude"]), radians(b["latitude"])
    lng = radians(b["longitude"] - a["longitude"])
    chord = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin(lng / 2) ** 2
    return 12742 * asin(min(1, sqrt(chord)))
