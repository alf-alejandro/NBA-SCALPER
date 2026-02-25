# NBA Edge Alpha Bot v3.0

Bot que detecta ineficiencias de precio en mercados NBA de Polymarket, gestiona posiciones simuladas con Take Profit / Stop Loss, y expone un dashboard web.

## Arquitectura

```
dashboard.py   → Servidor Flask (UI + API)
main.py        → Lógica del bot (scan, posiciones, scheduler)
/data/         → Persistencia (positions.json, scan_log.json, state.json)
```

## Variables de Entorno (Railway)

| Variable | Default | Descripción |
|---|---|---|
| `GEMINI_API_KEY` | — | API key de Google Gemini (requerida) |
| `PORT` | `8080` | Puerto del servidor web |
| `NEA_UMBRAL` | `10.0` | Puntos mínimos de NEA para abrir posición |
| `TAKE_PROFIT_DELTA` | `0.02` | +X sobre precio de entrada para TP (ej: 0.02 = +2¢) |
| `STOP_LOSS_DELTA` | `-0.05` | -X bajo precio de entrada para SL (ej: -0.05 = -5¢) |
| `MONITOR_INTERVAL` | `3600` | Segundos entre actualizaciones de precios (default 1h) |
| `DATA_DIR` | `/data` | Directorio de persistencia |
| `NBA_SERIES_ID` | `10345` | ID de la serie NBA en Polymarket |
| `GAMMA_API` | `https://gamma-api.polymarket.com` | URL de Gamma API |
| `CLOB_API` | `https://clob.polymarket.com` | URL de CLOB API |
| `GEMINI_MODEL` | `gemini-flash-lite-latest` | Modelo de Gemini |

## Comportamiento del Scheduler

1. **Scan automático**: Se ejecuta todos los días a las **9:00 AM ET**
2. **Scan manual**: Desde el botón "⚡ SCAN NOW" en el dashboard
3. **Monitoreo**: Actualiza precios de posiciones abiertas cada `MONITOR_INTERVAL` segundos

## Lógica de Posiciones

- **Abre posición** solo si `accion == "COMPRAR"` (precio bajo en Polymarket)
- **Take Profit**: cuando `precio_actual >= precio_entrada + TAKE_PROFIT_DELTA`
- **Stop Loss**: cuando `precio_actual <= precio_entrada + STOP_LOSS_DELTA`
- No abre duplicados para el mismo token activo

## Deploy en Railway

1. Crear proyecto en [railway.app](https://railway.app)
2. Conectar repositorio o subir archivos
3. Agregar **Volume** montado en `/data` para persistencia
4. Configurar variables de entorno
5. Deploy → Railway detecta el `Procfile` automáticamente

## Estructura de /data

```
/data/
  positions.json    → Lista de posiciones (abiertas y cerradas)
  scan_log.json     → Últimos 50 scans con resultados
  state.json        → Estado del scheduler (last_scan, manual_triggered)
```
