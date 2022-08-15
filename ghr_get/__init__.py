
def _main():
    try:
        from .cli import app
        app()
    except ImportError as e:
        print('The fancy command line app could not be loaded, and the fallback app is not yet written.', e)
        raise


if __name__ == '__main__':
    _main()
