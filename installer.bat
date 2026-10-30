@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo  Installation de Parle
echo  --------------------
echo.

rem Cherche Python (lanceur "py" ou "python")
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
    echo  Python est introuvable.
    echo  Installe-le depuis https://www.python.org/downloads/ puis relance ce fichier.
    echo  Pense a cocher "Add python.exe to PATH" pendant l'installation.
    pause
    exit /b 1
)

echo  1/2 Creation de l'environnement Python...
%PY% -m venv .venv || (echo  Echec de la creation de l'environnement. & pause & exit /b 1)

echo  2/2 Installation des bibliotheques (quelques minutes)...
.venv\Scripts\python.exe -m pip install --upgrade pip -q
.venv\Scripts\python.exe -m pip install -r requirements.txt -q || (echo  Echec de l'installation. & pause & exit /b 1)

echo.
echo  Installation terminee. Lancement de Parle...
echo  Le modele de reconnaissance (environ 500 Mo) se telecharge a la premiere dictee.
echo.
start "" ".venv\Scripts\pythonw.exe" "parle.py"
pause