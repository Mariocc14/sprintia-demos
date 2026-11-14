# Rastro: radar de leads en Reddit y Hacker News

Vigila Reddit y Hacker News 24/7 buscando gente que **pide lo que vendes**
("¿alguien recomienda una app para…?", "alternativa a X", "busco…"). Claude puntúa
cada post del 0 al 10 según su intención de compra y te propone una respuesta.
Te avisa por Telegram y lo gestionas todo desde un panel web.

**Nunca publica nada por ti.** Copias la respuesta, abres el post y la pegas tú.
Los mensajes y respuestas automáticos en Reddit van contra sus normas y acaban
en baneo de la cuenta.

## Qué incluye

- **Proyectos**: uno por producto, con descripción, cliente ideal, palabras clave,
  palabras excluidas, subreddits, búsquedas y nota mínima.
- **Autoconfiguración con IA**: pegas la web del producto y genera palabras clave,
  subreddits y búsquedas.
- **Escaneo automático** cada N minutos (posts y comentarios nuevos de tus
  subreddits, más búsquedas en todo Reddit y en Hacker News).
- **Filtro en dos pasos**: palabras clave (gratis) y luego Claude (solo con lo que pasa el primero).
- **Feed de leads** con nota, motivo, respuesta sugerida editable, «Copiar y abrir post»,
  «Otra respuesta», «Más corta» y estados (nuevo, guardado, respondido, descartado).
- **Avisos por Telegram** de los leads que superan la nota mínima.
- **Modo sin IA**: sin clave de Claude funciona con palabras clave y frases de intención.

## Probarlo en 1 minuto (modo demo, sin claves)

```bash
cd lead-radar
pip install -r requirements.txt
DEMO_MODE=1 uvicorn app:app --port 8000
# abre http://localhost:8000
```

## Ponerlo a funcionar de verdad

### 1. Claves

| Variable | Para qué | Cómo conseguirla |
|---|---|---|
| `ANTHROPIC_API_KEY` | Clasificar leads y escribir respuestas | console.anthropic.com → API Keys |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | Leer Reddit con la API oficial (sin ellas Reddit suele bloquear los servidores en la nube) | reddit.com/prefs/apps → «create another app» → tipo **script**. Es gratis para uso personal |
| `REDDIT_USER_AGENT` | Reddit pide identificarse | p. ej. `python:rastro:v1 (by /u/tu_usuario)` |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Avisos al móvil | Habla con @BotFather → `/newbot`. El chat id te lo da @userinfobot |
| `APP_PASSWORD` | Proteger el panel (usuario `admin`) | La que tú quieras. **Ponla siempre si lo publicas en internet** |
| `APP_NAME` | Nombre de la marca | Por defecto «Rastro» |

### 2. Desplegar (necesita estar encendido 24/7)

Cualquier servicio que ejecute un Dockerfile y tenga **disco persistente** para `/data`
(Railway, Render, Fly.io, un VPS…):

1. Crea el servicio apuntando a la carpeta `lead-radar/` (o a un repo con solo esta carpeta).
2. Añade un volumen montado en `/data`.
3. Configura las variables de la tabla.
4. Abre la URL, crea tus proyectos (o usa «Autoconfigurar») y pulsa «Buscar ahora».

En tu ordenador también funciona: `uvicorn app:app --port 8000` con las variables en el entorno.

### 3. Modo sin panel (opcional)

`python radar.py --config config.yaml` hace un escaneo, guarda un informe en `out/`
y avisa por Telegram. Útil para cron. Copia `config.example.yaml` como punto de partida.

## Coste aproximado de la IA

Solo se analizan los posts que pasan el filtro de palabras clave, en lotes de 15,
con la descripción de tus productos cacheada. Con 3 proyectos y unos 100 candidatos
al día, lo normal son pocos euros al mes con Claude Opus 5.5 y bastante menos con
Haiku 5.5 (se cambia en Ajustes). Revisa el consumo real en la consola de Anthropic
la primera semana.

## Buenas prácticas para no quemar tu cuenta de Reddit

- Responde solo cuando aportes algo de verdad y di que eres el creador del producto.
- Lee las normas de cada subreddit: muchos prohíben la autopromoción.
- No respondas a decenas de posts al día desde la misma cuenta.
- Usa una cuenta con historial real, no una recién creada.
