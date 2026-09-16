# Estructura del ebook — Upsell de Audiolibros (borrador de nombre: "The Free Library Method — Audiobook Edition")

Precio: (a definir — upsell/bump sobre el ebook de $19.99, no reemplazo)
Público: mismo lector angloparlante (UK, Canadá, Australia, Sudáfrica), ya sea que haya comprado el ebook de libros o entre directo por este bump.

Diferencia clave con el ebook de libros: el código de acceso es **independiente**
(`AUDIO2020`) — desbloquear audiolibros no destraba ebooks, y viceversa. Si el
comprador ya tiene el bot activado para ebooks, no reinstala nada: solo manda
`/unlock AUDIO2020` en el mismo chat.

---

## Índice completo

### Portada
- Mismo estilo visual que el ebook de libros, pero con ícono de audífonos/ondas
  de sonido en vez de pila de libros, para que se vea como "edición" del mismo
  producto, no algo separado.

### Introducción (1 página)
- Qué acaba de comprar: acceso a audiolibros gratis y legales, capítulo por
  capítulo, directo en el chat.
- Aclarar que es una función **separada** de la de ebooks (código distinto).
- Fuente: LibriVox (voluntarios leyendo libros de dominio público en voz alta).
- Qué NO es: no es texto-a-voz robótico, son lecturas humanas reales.

### Parte 1 — Instalación (si no tiene Telegram todavía)
- Idéntica a la del ebook de libros. Si ya lo compró, el comprador salta esto
  ("si ya tenés Telegram y el bot abierto, salteá a la Parte 2").

### Parte 2 — Activación
- Enviar `/unlock AUDIO2020` (código real, va horneado en la captura — no un
  placeholder).
- Confirmación esperada: "✅ Audiobooks unlocked! Try /audiobook <title>."
- Troubleshooting: código ya usado / typos / espacio faltante (igual que el de
  ebooks, pero aclarar que es un código DISTINTO al de ebooks — no confundir).

### Parte 3 — Cómo usar el bot (la diferencia más importante a explicar bien)
- **OJO:** a diferencia de ebooks, escribir el título solo NO alcanza — hay que
  usar el comando `/audiobook <título>`. Si el comprador escribe el título
  pelado, el bot lo va a tratar como búsqueda de ebook (y le va a decir que esa
  función está bloqueada si no la compró). Esto hay que remarcarlo fuerte para
  evitar tickets de soporte.
- Elegir de la lista numerada de resultados.
- Ver la lista de capítulos (paginada) y tocar el número del capítulo para
  recibirlo como archivo de audio individual — se reproduce nativo en Telegram
  (play/pause, barra de progreso), no como documento genérico.
- Botón "📦 Download all (ZIP)" para bajar el audiolibro completo de una vez.
- `/popularaudio` — 10 audiolibros clásicos listos sin buscar nada.
- Límite conocido de búsqueda (prefijo literal, LibriVox a veces omite el
  artículo inicial) — mismo tip que en el código: probar sin "The"/"A"/"An", o
  buscar solo el apellido del autor.

### Parte 4 — Escuchar en tu dispositivo
- Más simple que el de ebooks (no hay problema de formato tipo Kindle): es un
  MP3 estándar, funciona en cualquier lado.
- Escuchar directo en Telegram (recomendado, cero pasos extra).
- Cómo guardar un capítulo al teléfono para escucharlo sin abrir Telegram.
- Tip de auriculares / auto (Bluetooth) — mismo player de Telegram sirve como
  reproductor mientras se maneja o camina.
- Extraer el ZIP completo en la computadora si quiere pasar todos los
  capítulos a otra app (ej. un reproductor de música).

### Parte 5 — Bonus
- Lista de ~40-50 audiolibros clásicos para probar (misma lista de dominio
  público que en el ebook de libros — LibriVox tiene lecturas de casi todos
  estos títulos).
- Qué hacer si un audiolibro no aparece (mismo tip: apellido del autor, sacar
  "The"/"A" del título).
- Soporte por Instagram.

### Cierre
- Recordatorio de que `/reset` limpia el chat sin afectar el acceso.
- Agradecimiento.

---

## Brief de imagen para el prompt de portada (Gemini / Higgsfield)

> "A warm, cinematic photo of someone relaxing with wireless earbuds in, eyes
> closed, a smartphone glowing softly in their hand, cozy home or park setting
> at golden hour, soft bokeh lighting, editorial photography style, no visible
> text, logos, or UI on the phone screen, warm amber and cream color palette,
> shallow depth of field — same visual family as a cozy reading-nook photo,
> but centered on listening instead of reading."

(Evita pedir texto/UI específica en la imagen por la misma razón que en el
ebook de libros: los modelos de imagen fallan mucho ahí — el título y
subtítulo se agregan después como texto, no generado por la IA de imagen.)
