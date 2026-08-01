#!/usr/bin/env bash
# Instalador de AGOR. Se puede ejecutar varias veces sin problema.
set -euo pipefail
cd "$(dirname "$0")"

echo "──────────────────────────────────────────────"
echo " Instalando AGOR (radar de oportunidades)"
echo "──────────────────────────────────────────────"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: no encuentro python3."
  echo "Instálalo desde https://www.python.org/downloads/ y vuelve a ejecutar este script."
  exit 1
fi

VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python detectado: $VERSION"

if [ ! -d .venv ]; then
  echo "Creando entorno aislado en .venv …"
  python3 -m venv .venv
fi

echo "Instalando dependencias …"
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "He creado el fichero .env. TIENES QUE EDITARLO antes de continuar:"
  echo "   1. SEC_USER_AGENT  → pon tu nombre y tu email (obligatorio, la SEC lo exige)"
  echo "   2. POLYGON_API_KEY → clave gratuita en https://polygon.io/dashboard/api-keys"
  echo ""
  echo "Ábrelo con:  open -e .env"
else
  echo "El fichero .env ya existe, no lo toco."
fi

chmod +x radar 2>/dev/null || true

echo ""
echo "──────────────────────────────────────────────"
echo " Instalación terminada."
echo ""
echo " Siguiente paso:  ./radar estado"
echo "──────────────────────────────────────────────"
