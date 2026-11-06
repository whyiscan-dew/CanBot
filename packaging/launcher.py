"""PyInstaller giriş noktası: CanBot.exe bu dosyadan üretilir."""

import sys

from bot.desktop import main

if __name__ == "__main__":
    sys.exit(main())
