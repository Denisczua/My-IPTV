# My-IPTV
Free auto-updating IPTV playlist for OTTPlayer

## Основной плейлист и киноканалы

Действующий адрес сохраняется: [playlist.m3u](https://raw.githubusercontent.com/Denisczua/My-IPTV/main/playlist.m3u).
Все дополнительные источники и резервные киноканалы интегрируются в этот файл.
[Архитектура, проверка из Чехии и Windows-агент](docs/MAIN-MOVIES.md) ·
[Отчёт по киноканалам](playlist-movies-report.md).
Обновление: `python scripts/update_playlist.py`.

## Русский и украинский плейлисты для Чехии

- `playlist-ru-ua-testing.m3u` — языковые кандидаты UNKNOWN_CZ/UNSTABLE_CZ.
- `playlist-ru-ua.m3u` появится после повторных успешных проверок STABLE_CZ
  с прямого чешского IP. Пока таких проверок нет, основной файл не публикуется.
- [Инструкция для Windows, автоматизация и правила проверки](docs/RU-UA-WINDOWS.md).

Прежние RU/UA-представления сохраняются и обновляются единым workflow. Ручное обновление производного каталога:
`python scripts/filter_ru_ua.py --refresh-metadata`.
