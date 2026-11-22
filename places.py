"""Offline place recognition from GPS coordinates (nearest city/village, no internet needed)."""
import gzip, math
from pathlib import Path

DATA = Path(__file__).resolve().parent / "cities.csv.gz"

COUNTRIES = {
    "MK": "North Macedonia", "GR": "Greece", "AL": "Albania", "RS": "Serbia", "BG": "Bulgaria", "XK": "Kosovo",
    "ME": "Montenegro", "BA": "Bosnia and Herzegovina", "HR": "Croatia", "SI": "Slovenia", "TR": "Turkey",
    "IT": "Italy", "DE": "Germany", "AT": "Austria", "CH": "Switzerland", "FR": "France", "ES": "Spain",
    "PT": "Portugal", "NL": "Netherlands", "BE": "Belgium", "GB": "United Kingdom", "IE": "Ireland",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland", "PL": "Poland", "CZ": "Czechia",
    "SK": "Slovakia", "HU": "Hungary", "RO": "Romania", "UA": "Ukraine", "RU": "Russia", "CY": "Cyprus",
    "MT": "Malta", "LU": "Luxembourg", "US": "USA", "CA": "Canada", "AU": "Australia", "AE": "UAE",
    "EG": "Egypt", "TN": "Tunisia", "MA": "Morocco", "TH": "Thailand", "JP": "Japan", "CN": "China",
}

# Well-known cities: when one is within ~14 km it is preferred over a nearby suburb or village.
KNOWN = set(['AE', 'AL', 'AT', 'AU', 'Amsterdam', 'Antalya', 'Aracinovo', 'Asprovalta', 'Athens', 'BA', 'BE', 'BG', 'Bansko', 'Barcelona', 'Bari', 'Belgrade', 'Berlin', 'Berovo', 'Bitola', 'Blagoevgrad', 'Bodrum', 'Bogdanci', 'Brussels', 'Bucharest', 'Budapest', 'Budva', 'Burgas', 'CA', 'CH', 'CN', 'CY', 'CZ', 'Chalkidiki', 'Chania', 'Chicago', 'Cologne', 'Copenhagen', 'Corfu', 'DE', 'DK', 'Debar', 'Delcevo', 'Delchevo', 'Demir Hisar', 'Demir Kapija', 'Dubai', 'Dubrovnik', 'Durres', 'EG', 'ES', 'Edessa', 'Edirne', 'FI', 'FR', 'Florence', 'Florina', 'Frankfurt am Main', 'GB', 'GR', 'Geneva', 'Gevgelija', 'Gostivar', 'HR', 'HU', 'Hamburg', 'Heraklion', 'IE', 'IT', 'Ilinden', 'Ioannina', 'Istanbul', 'Izmir', 'JP', 'Kalamaria', 'Kallithea', 'Kastoria', 'Katerini', 'Kavadarci', 'Kavala', 'Kerkyra', 'Kicevo', 'Kichevo', 'Kocani', 'Kochani', 'Korce', 'Kotor', 'Kratovo', 'Kriva Palanka', 'Krusevo', 'Krushevo', 'Ksamil', 'Kumanovo', 'Kusadasi', 'LU', 'Larisa', 'Lefkada', 'Leptokarya', 'Lisbon', 'Ljubljana', 'London', 'MA', 'ME', 'MK', 'MT', 'Madrid', 'Makedonska Kamenica', 'Makedonski Brod', 'Malmo', 'Mavrovo', 'Melbourne', 'Milan', 'Mostar', 'Munich', 'NL', 'NO', 'Naples', 'Nea Moudania', 'Negotino', 'Neoi Poroi', 'Nesebar', 'New York City', 'Nice', 'Nis', 'Novi Sad', 'Ohrid', 'Oslo', 'PL', 'PT', 'Paralia', 'Paris', 'Pehcevo', 'Petrovec', 'Platamon', 'Plovdiv', 'Podgorica', 'Pogradec', 'Polychrono', 'Prague', 'Prilep', 'Pristina', 'Prizren', 'Probishtip', 'RO', 'RS', 'RU', 'Radovish', 'Resen', 'Rhodes', 'Rome', 'SE', 'SI', 'SK', 'Saraj', 'Sarajevo', 'Sarande', 'Sarti', 'Shtip', 'Skopje', 'Sofia', 'Sopishte', 'Split', 'Star Dojran', 'Stavros', 'Stockholm', 'Struga', 'Strumica', 'Studenichani', 'Stuttgart', 'Sunny Beach', 'Sveti Nikole', 'Sydney', 'TH', 'TN', 'TR', 'Tetovo', 'Thessaloniki', 'Tirana', 'Toronto', 'UA', 'US', 'Ulcinj', 'Valandovo', 'Valletta', 'Varna', 'Veles', 'Venice', 'Vevcani', 'Vienna', 'Vinica', 'Vlore', 'Vranje', 'Warsaw', 'XK', 'Zagreb', 'Zelenikovo', 'Zurich'])


class Places:
    def __init__(self):
        self.grid = {}
        self.ok = False
        try:
            with gzip.open(DATA, "rt", encoding="utf-8") as f:
                for line in f:
                    lat, lon, cc, name = line.rstrip("\n").split(",", 3)
                    lat, lon = float(lat), float(lon)
                    self.grid.setdefault((int(math.floor(lat)), int(math.floor(lon))), []).append((lat, lon, cc, name))
            self.ok = True
        except Exception as e:
            print("Place data not loaded:", e)

    def lookup(self, lat, lon):
        """Returns (place, country) or (None, None)."""
        if not self.ok or lat is None or lon is None:
            return None, None
        best, bd, known, kd = None, 1e18, None, 1e18
        gy, gx = int(math.floor(lat)), int(math.floor(lon))
        coslat = math.cos(math.radians(lat))
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for c in self.grid.get((gy + dy, gx + dx), ()):
                    d = (c[0] - lat) ** 2 + ((c[1] - lon) * coslat) ** 2
                    if d < bd:
                        bd, best = d, c
                    if d < kd and c[3] in KNOWN:
                        kd, known = d, c
        if not best or bd > 0.6 ** 2:  # farther than ~65 km from any place: sea, desert…
            return None, None
        if known and kd < 0.13 ** 2:  # a known city nearby (~14 km) wins over a suburb or village
            best = known
        _, _, cc, name = best
        return name, COUNTRIES.get(cc, cc)
