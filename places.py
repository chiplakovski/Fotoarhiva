"""Офлајн препознавање на место од GPS координати (најблизок град/село, без интернет)."""
import gzip, math
from pathlib import Path

DATA = Path(__file__).resolve().parent / "cities.csv.gz"

COUNTRIES = {
    "MK": "Македонија", "GR": "Грција", "AL": "Албанија", "RS": "Србија", "BG": "Бугарија", "XK": "Косово",
    "ME": "Црна Гора", "BA": "Босна и Херцеговина", "HR": "Хрватска", "SI": "Словенија", "TR": "Турција",
    "IT": "Италија", "DE": "Германија", "AT": "Австрија", "CH": "Швајцарија", "FR": "Франција", "ES": "Шпанија",
    "PT": "Португалија", "NL": "Холандија", "BE": "Белгија", "GB": "Велика Британија", "IE": "Ирска",
    "SE": "Шведска", "NO": "Норвешка", "DK": "Данска", "FI": "Финска", "PL": "Полска", "CZ": "Чешка",
    "SK": "Словачка", "HU": "Унгарија", "RO": "Романија", "UA": "Украина", "RU": "Русија", "CY": "Кипар",
    "MT": "Малта", "LU": "Луксембург", "US": "САД", "CA": "Канада", "AU": "Австралија", "AE": "ОАЕ",
    "EG": "Египет", "TN": "Тунис", "MA": "Мароко", "TH": "Тајланд", "JP": "Јапонија", "CN": "Кина",
}

# Точни кирилични имиња за поголемите места во Македонија
MK_NAMES = {
    "Skopje": "Скопје", "Ohrid": "Охрид", "Struga": "Струга", "Bitola": "Битола", "Kumanovo": "Куманово",
    "Prilep": "Прилеп", "Tetovo": "Тетово", "Veles": "Велес", "Shtip": "Штип", "Kochani": "Кочани",
    "Kocani": "Кочани", "Gostivar": "Гостивар", "Kavadarci": "Кавадарци", "Strumica": "Струмица",
    "Kichevo": "Кичево", "Kicevo": "Кичево", "Gevgelija": "Гевгелија", "Debar": "Дебар", "Kriva Palanka": "Крива Паланка",
    "Sveti Nikole": "Свети Николе", "Negotino": "Неготино", "Radovish": "Радовиш", "Delchevo": "Делчево",
    "Delcevo": "Делчево", "Vinica": "Виница", "Resen": "Ресен", "Probishtip": "Пробиштип", "Berovo": "Берово",
    "Kratovo": "Кратово", "Makedonski Brod": "Македонски Брод", "Valandovo": "Валандово", "Demir Hisar": "Демир Хисар",
    "Krushevo": "Крушево", "Krusevo": "Крушево", "Star Dojran": "Стар Дојран", "Bogdanci": "Богданци",
    "Pehcevo": "Пехчево", "Makedonska Kamenica": "Македонска Каменица", "Demir Kapija": "Демир Капија",
    "Vevcani": "Вевчани", "Mavrovo": "Маврово", "Saraj": "Сарај", "Aracinovo": "Арачиново", "Petrovec": "Петровец",
    "Ilinden": "Илинден", "Zelenikovo": "Зелениково", "Sopishte": "Сопиште", "Studenichani": "Студеничани",
}


# Познати места надвор од Македонија, на македонски
WORLD_NAMES = {
    "Athens": "Атина", "Thessaloniki": "Солун", "Kalamaria": "Солун", "Kavala": "Кавала", "Florina": "Лерин", "Edessa": "Воден",
    "Kastoria": "Костур", "Katerini": "Катерини", "Paralia": "Паралија", "Leptokarya": "Лептокарија", "Neoi Poroi": "Неи Пори",
    "Platamon": "Платамонас", "Polychrono": "Полихроно", "Kallithea": "Калитеја", "Nea Moudania": "Неа Муданија",
    "Sarti": "Сарти", "Asprovalta": "Аспровалта", "Stavros": "Ставрос", "Chalkidiki": "Халкидики", "Corfu": "Крф", "Kerkyra": "Крф",
    "Lefkada": "Лефкада", "Rhodes": "Родос", "Heraklion": "Ираклион", "Chania": "Ханја", "Larisa": "Лариса", "Ioannina": "Јанина",
    "Tirana": "Тирана", "Durres": "Драч", "Sarande": "Саранда", "Vlore": "Валона", "Pogradec": "Поградец", "Korce": "Корча",
    "Ksamil": "Ксамил", "Belgrade": "Белград", "Novi Sad": "Нови Сад", "Nis": "Ниш", "Vranje": "Врање", "Sofia": "Софија",
    "Plovdiv": "Пловдив", "Varna": "Варна", "Burgas": "Бургас", "Blagoevgrad": "Благоевград", "Sunny Beach": "Сончев Брег",
    "Nesebar": "Несебар", "Bansko": "Банско", "Pristina": "Приштина", "Prizren": "Призрен", "Podgorica": "Подгорица",
    "Budva": "Будва", "Kotor": "Котор", "Ulcinj": "Улцињ", "Sarajevo": "Сараево", "Mostar": "Мостар", "Zagreb": "Загреб",
    "Split": "Сплит", "Dubrovnik": "Дубровник", "Ljubljana": "Љубљана", "Istanbul": "Истанбул", "Edirne": "Одрин",
    "Antalya": "Анталија", "Izmir": "Измир", "Bodrum": "Бодрум", "Kusadasi": "Кушадаси", "Rome": "Рим", "Milan": "Милано",
    "Venice": "Венеција", "Florence": "Фиренца", "Naples": "Неапол", "Bari": "Бари", "Vienna": "Виена", "Berlin": "Берлин",
    "Munich": "Минхен", "Hamburg": "Хамбург", "Frankfurt am Main": "Франкфурт", "Cologne": "Келн", "Stuttgart": "Штутгарт",
    "Zurich": "Цирих", "Geneva": "Женева", "Paris": "Париз", "Nice": "Ница", "Madrid": "Мадрид", "Barcelona": "Барселона",
    "Lisbon": "Лисабон", "Amsterdam": "Амстердам", "Brussels": "Брисел", "London": "Лондон", "Prague": "Прага",
    "Budapest": "Будимпешта", "Warsaw": "Варшава", "Bucharest": "Букурешт", "Dubai": "Дубаи", "New York City": "Њујорк",
    "Chicago": "Чикаго", "Toronto": "Торонто", "Sydney": "Сиднеј", "Melbourne": "Мелбурн", "Malmo": "Малме",
    "Stockholm": "Стокхолм", "Copenhagen": "Копенхаген", "Oslo": "Осло", "Valletta": "Валета",
}


def translit_mk(s):
    pairs = [("dzh", "џ"), ("Dzh", "Џ"), ("dz", "ѕ"), ("Dz", "Ѕ"), ("gj", "ѓ"), ("Gj", "Ѓ"), ("kj", "ќ"), ("Kj", "Ќ"),
             ("lj", "љ"), ("Lj", "Љ"), ("nj", "њ"), ("Nj", "Њ"), ("zh", "ж"), ("Zh", "Ж"), ("ch", "ч"), ("Ch", "Ч"),
             ("sh", "ш"), ("Sh", "Ш"), ("kh", "х"), ("Kh", "Х"), ("ts", "ц"), ("Ts", "Ц")]
    for a, b in pairs:
        s = s.replace(a, b)
    single = dict(zip("abvgdejzijklmnoprstufhcABVGDEJZIKLMNOPRSTUFHC", "абвгдејзијклмнопрстуфхцАБВГДЕЈЗИКЛМНОПРСТУФХЦ"))
    return "".join(single.get(ch, ch) for ch in s)


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
            print("Податоците за места не се вчитани:", e)

    def lookup(self, lat, lon):
        """Враќа (место, држава) или (None, None)."""
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
                    if d < kd and (c[3] in WORLD_NAMES or (c[2] == "MK" and c[3] in MK_NAMES)):
                        kd, known = d, c
        if not best or bd > 0.6 ** 2:  # подалеку од ~65 km од било кое место: море, пустина…
            return None, None
        if known and kd < 0.13 ** 2:  # познат град во близина (~14 km) има предност пред предградие/село
            best = known
        _, _, cc, name = best
        if cc == "MK":
            name = MK_NAMES.get(name) or translit_mk(name)
        else:
            name = WORLD_NAMES.get(name, name)
        return name, COUNTRIES.get(cc, cc)
