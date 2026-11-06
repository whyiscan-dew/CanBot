@echo off
chcp 65001 >nul
title CanBot kurulumu
cd /d "%~dp0"
echo.
echo  CanBot kurulumu (kaynak koddan)
echo  ================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")
%PY% --version >nul 2>nul
if not %errorlevel%==0 (
  echo  [HATA] Python bulunamadi.
  echo  https://www.python.org/downloads/ adresinden Python 3.11 veya uzerini kurun
  echo  ve kurulumda "Add python.exe to PATH" kutusunu isaretleyin.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo  Sanal ortam olusturuluyor...
  %PY% -m venv .venv || goto :hata
)

echo  Gerekli paketler yukleniyor (birkac dakika surebilir)...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt -r requirements-desktop.txt || goto :hata

echo  Masaustune kisayol olusturuluyor...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\CanBot.lnk');" ^
  "$s.TargetPath='%CD%\.venv\Scripts\pythonw.exe';" ^
  "$s.Arguments='\"%CD%\CanBot.pyw\"';" ^
  "$s.WorkingDirectory='%CD%';" ^
  "$s.IconLocation='%CD%\bot\web\static\icons\canbot.ico';" ^
  "$s.Description='CanBot otomatik islem botu';" ^
  "$s.Save()"

echo.
echo  Kurulum tamamlandi. CanBot baslatiliyor...
start "" ".venv\Scripts\pythonw.exe" "%CD%\CanBot.pyw"
echo  Bundan sonra masaustundeki CanBot simgesiyle acabilirsiniz.
timeout /t 5 >nul
exit /b 0

:hata
echo.
echo  [HATA] Kurulum tamamlanamadi. Internet baglantinizi kontrol edip tekrar deneyin.
pause
exit /b 1
