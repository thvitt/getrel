def _main():
    try:
        from .cli import main

        main()
    except ImportError as e:
        from .simplecli import main

        main()


if __name__ == "__main__":
    _main()
