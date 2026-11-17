# Образцы тестов ballbar для разбора .b5r

Сюда кладутся реальные тесты Renishaw QC20 / Ballbar 20, по три файла на тест с одним именем:

| Файл | Что это |
|---|---|
| `<имя>.b5r` | оригинальный файл результатов Ballbar 20 |
| `<имя>.pdf` | PDF-отчёт Ballbar 20 по этому же тесту (источник эталонных чисел) |
| `<имя>.expected.json` | числа из отчёта, переписанные вручную |

Формат `<имя>.expected.json`:

```json
{
  "setup": {"plane": "XY", "radius_mm": 100, "feed_mm_min": 1000, "arc_deg": 360},
  "results": [
    {"parameter": "circularity", "axis": null, "direction": null, "value": 9.8},
    {"parameter": "squareness", "axis": null, "direction": null, "value": -12.4},
    {"parameter": "backlash", "axis": "X", "direction": null, "value": 2.1}
  ],
  "abs_tolerance": 0.05
}
```

Коды параметров, оси и направления — из `app/geometry/catalog.py`, единицы — как в каталоге
(µm, µm/m, ppm, ms). `abs_tolerance` — допустимое расхождение из-за округления в отчёте.

Golden-тест `tests/golden/test_b5r_parser.py` проходит по всем образцам: файл → числа отчёта.
Пока образцов нет, он пропускается; с первым образцом он упадёт, пока не написан разбор
(`app/geometry/b5r.py`). Желательно несколько образцов: разные плоскости, 360° и 220°,
разные радиусы и подачи.
