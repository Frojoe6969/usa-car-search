import importlib.util
import unittest
from pathlib import Path


def load_script():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("usa_car_search_script", root / "usa-car-search.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoreLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_script()

    def test_color_filter_uses_allowed_colors(self):
        self.assertTrue(self.app.color_matches_str("Crystal Black Silica"))
        self.assertTrue(self.app.color_matches_str("Magnetite Gray Metallic"))
        self.assertFalse(self.app.color_matches_str("Pure Red"))

    def test_trim_filter_defaults_to_all_trims(self):
        self.assertTrue(self.app.trim_matches("Base"))
        self.assertTrue(self.app.trim_matches("Limited"))
        self.assertTrue(self.app.trim_matches(""))

    def test_haversine_distance_is_reasonable(self):
        miles = self.app.haversine_miles(40.7128, -74.0060, 42.3601, -71.0589)
        self.assertGreater(miles, 180)
        self.assertLess(miles, 230)

    def test_score_deals_fills_missing_ratings(self):
        listings = [
            {"id": "a", "price": 10000, "deal": ""},
            {"id": "b", "price": 12000, "deal": ""},
            {"id": "c", "price": 15000, "deal": ""},
        ]
        scored = self.app.score_deals(listings)
        self.assertEqual(scored[0]["deal"], "Great Deal")
        self.assertEqual(scored[1]["deal"], "Fair Deal")
        self.assertEqual(scored[2]["deal"], "Overpriced")

    def test_dedupe_prefers_unique_vins_and_fingerprints(self):
        listings = [
            {"id": "one", "vin": "JF1VA1A60K9800001", "year": 2020, "mileage": 30000, "price": 25000, "source": "A"},
            {"id": "two", "vin": "JF1VA1A60K9800001", "year": 2020, "mileage": 30000, "price": 25000, "source": "B"},
            {"id": "three", "vin": "", "year": 2020, "mileage": 30000, "price": 25200, "source": "C"},
            {"id": "four", "vin": "", "year": 2020, "mileage": 35000, "price": 26000, "source": "D"},
        ]
        deduped = self.app.dedupe_listings(listings)
        self.assertEqual([item["id"] for item in deduped], ["one", "four"])


if __name__ == "__main__":
    unittest.main()