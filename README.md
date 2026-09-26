# Radar Global

Screener cross-asset semanal. Cada domingo a las ~6:15 p. m. (Colombia) mide ~60 instrumentos (acciones por región,
bonos soberanos, divisas, materias primas, energía, crédito y ratios macro) contra su propia normalidad
de 3 años, detecta extremos, revisa qué pasó históricamente tras extremos parecidos, cruza con el
calendario electoral y te entrega:

- **Análisis de Claude** (tarea programada, domingo ~7:50 p. m.): lee los resultados, busca la causa de cada alerta, actualiza las encuestas electorales y te entrega el informe.
- **Correo** (opcional, si configuras los secrets de Gmail).
- **Página web** (GitHub Pages) con todo el detalle, tooltips y el archivo de semanas anteriores.

Corre 100% en la nube de GitHub, gratis. No necesitas tener nada encendido.

---

## Montaje (una sola vez, ~15 minutos, mejor desde el computador)

### 1. Crear el repositorio
1. En github.com → **New repository** → nombre `radar-global` → **Public** → Create.
   (Público porque GitHub Pages es gratis solo en repos públicos. No contiene datos personales:
   tu correo y la clave van como *secrets*, que nadie ve.)
2. **Add file → Upload files** → arrastra *todo el contenido* de esta carpeta
   (incluida la carpeta oculta `.github`). Commit.
   > Si el explorador no deja ver `.github`: crea el archivo a mano con
   > **Add file → Create new file**, nombre `.github/workflows/radar.yml`, y pega el contenido.

### 2. Clave de Gmail para el envío
1. Tu cuenta de Google debe tener verificación en dos pasos activa.
2. Ve a <https://myaccount.google.com/apppasswords> → crea una clave llamada "Radar" → copia los 16 caracteres.

### 3. Secrets del repositorio
**Settings → Secrets and variables → Actions → New repository secret**:

| Nombre | Valor |
|---|---|
| `GMAIL_USER` | tu correo de Gmail |
| `GMAIL_APP_PASSWORD` | la clave de 16 caracteres del paso 2 |
| `MAIL_TO` | (opcional) destinatario(s), separados por coma. Si no lo pones, llega a `GMAIL_USER` |
| `FRED_API_KEY` | (muy recomendado) clave gratuita de FRED: <https://fredaccount.stlouisfed.org/apikeys>. Sin ella, las tasas y los spreads de crédito pueden no descargarse desde GitHub |

### 4. Activar la página web
**Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `main` / carpeta `/docs`** → Save.
Tu radar quedará en `https://TU-USUARIO.github.io/radar-global/`.

### 5. Primera corrida
**Actions → Radar semanal → Run workflow**. En 2–4 minutos te llega el correo y la página se actualiza.
Desde ahí corre solo cada domingo.

---

## Uso diario (desde el celular)
- **Leer:** el correo del domingo, o la página (guárdala en la pantalla de inicio).
- **Correr fuera de horario:** app de GitHub o github.com → Actions → Radar semanal → Run workflow.
- **Ajustar umbrales, agregar activos o editar escenarios electorales:** abre `config.yaml` o
  `elecciones.yaml` en github.com → ícono de lápiz → Commit. Aplica desde la siguiente corrida.

## Qué calcula

| Métrica | Qué es |
|---|---|
| **z-score** | Distancia a la normalidad del activo en desviaciones estándar (ventana de 3 años). Precios y divisas: distancia a su tendencia de 40 semanas. Tasas, spreads, ratios y volatilidad: nivel. |
| **Niveles** | Aviso ≥ 2.0 · Fuerte ≥ 2.5 · Extremo ≥ 3.0 (paramétricos). |
| **Percentil 10a** | Dónde está el nivel actual frente a los últimos ~10 años. |
| **Confiabilidad** | Veces anteriores que el activo entró en extremo en la misma dirección, y qué pasó 13 semanas después: % que revirtió y movimiento mediano. Si revirtió menos del 50%, ese extremo ha sido más tendencia que oportunidad. |
| **Elecciones** | Aviso a ≤ 90 días, con el z de los activos asociados: si ya están en extremo, el mercado probablemente descontó parte del evento. |

## Archivos
- `config.yaml` — universo, tooltips ("por qué está aquí", "qué te cuenta") y parámetros.
- `elecciones.yaml` — calendario electoral (revisado 26-sep-2026). Actualízalo cuando se fijen nuevas fechas.
- `screener.py` — el motor. `python screener.py --demo --no-email` genera una página de prueba con datos simulados.
- `docs/` — la página publicada y `docs/archivo/` con cada semana.
- `data/historial.csv` — z-score de cada instrumento en cada corrida (para análisis futuro).

## Límites a tener presentes
- Fuentes gratuitas (Yahoo Finance, FRED). Yahoo a veces falla o limita; el radar lo reporta en
  "Calidad de datos" y sigue con lo que tenga.
- Los bonos de Alemania, Japón y Francia son series mensuales de la OCDE: llegan con semanas de rezago.
- Los futuros continuos (petróleo, café, etc.) tienen saltos en los cambios de contrato.
- GitHub desactiva flujos programados tras 60 días sin actividad en el repo; como el radar hace un
  commit cada semana, eso no debería pasar. Si un domingo no llega el correo, revisa la pestaña Actions.
- **No es una recomendación de inversión.** Es un radar de desajustes estadísticos para decidir qué investigar.
