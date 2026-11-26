"""The single entry point updating the existing OTTPlayer playlist."""
from main_catalogue import run
from filter_ru_ua import lock, record_failure


def main():
    with lock():
        return run(refresh=True, publish=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        record_failure('main-playlist', error)
        raise
