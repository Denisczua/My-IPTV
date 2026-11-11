# My-IPTV
Free auto-updating IPTV playlist for OTTPlayer

## Русский и украинский плейлисты для Чехии

- `playlist-ru-ua-testing.m3u` — языковые кандидаты UNKNOWN_CZ/UNSTABLE_CZ.
- `playlist-ru-ua.m3u` появится после повторных успешных проверок STABLE_CZ
  с прямого чешского IP. Пока таких проверок нет, основной файл не публикуется.
- [Инструкция для Windows, автоматизация и правила проверки](docs/RU-UA-WINDOWS.md).

Исходный `playlist.m3u` сохраняется. Обновление каталога:
`python scripts/filter_ru_ua.py --refresh-metadata`.
