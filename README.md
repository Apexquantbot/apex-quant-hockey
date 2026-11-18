# Apex Quant Hockey (NHL)

Versión de Apex Quant para hockey hielo: **NHL** (temporada regular y playoffs).
Misma familia que Apex Quant fútbol, basket y tenis. Se ve dentro de **Apex Quant Deportes** (pestaña 🏒).

- Web / app: https://apexquantbot.github.io/apex-quant-hockey/
- Canal de Telegram: **pendiente** (de momento el bot funciona sin Telegram; todo se ve en la app)

## Qué hace (sin cuotas de casas)

Este bot **no consulta cuotas** (no usa The Odds API ni gasta créditos). Calcula con su modelo la probabilidad
de cada mercado y la **cuota justa** (1 ÷ probabilidad). Tú la comparas con Winamax:

- **Cuota justa**: la que pagaría una casa sin margen, según el modelo.
- **Cuota mínima** = justa + 5 %. Si Winamax paga esa cuota o más, hay valor.
- En la web, en cada partido, toca **«Cuotas justas»** y escribe la cuota de Winamax en la casilla: te dice al momento la ventaja.

Mercados: ganador (prórroga incluida), 1X2 a 60', doble oportunidad, total de goles 4.5/5.5/6.5 (a 60' y con prórroga),
hándicap ±1.5, ambos marcan y goles de cada equipo.

## Cuándo avisa (cuando se active Telegram)

Sin los secretos de Telegram el bot hace todo igual (previa, pronósticos «⭐ Destacados», resultados) pero no manda mensajes.
Para activarlo: crear el canal, poner los 2 secretos de abajo y escribir el nombre del canal en `TG_CANAL` de `index.html`.


- **Previa de la noche** en Telegram hacia las **18:00** (hora de España): todos los partidos con las cuotas justas
  de los mercados principales y hasta **6 pronósticos** del modelo con su cuota mínima.
  (Los partidos de la NHL son de madrugada en España, por eso la previa va por la tarde.)
- **Resultados** por la mañana, al terminar los partidos.
- **Resumen semanal** el lunes por la mañana y **mensual** el día 1.

## Cómo funciona

| Pieza | Qué hace |
|---|---|
| `bot_quant.py` | El bot. Se ejecuta cada 15 minutos con GitHub Actions. |
| `datos.json` | Lo que muestra la web (partidos, cuotas justas, pronósticos). Lo genera el bot. |
| `equipos.json` | Resultados de los últimos ~400 días (temporada actual y anterior). Lo genera el bot. |
| `historico_picks.json` / `historico_mensual.json` | Libro de todos los pronósticos liquidados. |
| `calibracion.json` | Lo que dijo el modelo de cada mercado y si salió: para comprobar si las cuotas justas son de fiar. |

**Fuente de datos (gratis):** ESPN (calendario, directo y resultados).

**Modelo:**
1. Ataque y defensa de cada equipo con los goles a 60' (los partidos recientes pesan más; uno de hace 75 días pesa la mitad).
2. Goles esperados de cada equipo, con ventaja de jugar en casa y descuento si jugó la noche anterior.
3. Distribución de Poisson (con un poco más de empates a 60', como pasa en el hockey) → todos los mercados.
4. Si hay empate a los 60', prórroga/penaltis según la fuerza de cada equipo (el ganador suma 1 gol, norma NHL).

**Lo que el modelo NO sabe:** qué portero es titular, lesiones ni bajas de última hora. Revísalo antes de apostar.

## Secretos del repositorio (Settings → Secrets and variables → Actions)

| Nombre | Obligatorio | Valor |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Cuando se active Telegram | Token del bot de Telegram (puede ser el mismo bot del tenis o el basket) |
| `TELEGRAM_CHAT_ID` | Cuando se active Telegram | `@apex_quant_hockey` (o el nombre del canal que crees) |

Ajustes opcionales (variables de entorno en `bot.yml`): `PREVIA_HORA` (18), `MARGEN_VALOR` (1.05), `PICKS_MAX_DIA` (6).

## Lanzamiento cada 15 minutos (cron-job.org)

- URL: `https://api.github.com/repos/Apexquantbot/apex-quant-hockey/actions/workflows/bot.yml/dispatches`
- Método POST · cabeceras `Authorization: Bearer <token>` y `Accept: application/vnd.github+json` · cuerpo `{"ref":"main"}`

🔞 +18 · Juega con responsabilidad · Información estadística, no es asesoramiento.
