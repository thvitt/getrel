
def _main():
    try:
        from .cli import app
        app()
    except ImportError as e:
        from .simplecli import main 
        main()

if __name__ == '__main__':
    _main()
