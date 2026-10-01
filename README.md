# Registro de visitas con selfie

MVP local para recibir solicitudes de visita, revisar selfies y validar manualmente el código del trabajador desde un panel administrativo.

## Requisitos e inicio

- Python 3.12 o superior (requerido por el detector preentrenado de gafas).
- Navegador con acceso a cámara. `getUserMedia` funciona en `localhost`; para otros equipos se necesita HTTPS.

En PowerShell, desde la carpeta del proyecto:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Completa `.env` con una clave aleatoria, el token de un bot de Telegram y el chat ID privado del administrador. Para iniciar una conversación, primero abre el bot y envíale `/start`.

```powershell
python app.py
```

Abre `http://127.0.0.1:5000/` para el registro y `http://127.0.0.1:5000/admin` para el panel. El archivo `.env` se carga al iniciar el servidor.

## Logo de la empresa

Guarda el logo en `static/company-logo.png` (ruta completa en Windows: `C:\xampp\htdocs\scammers\selfie\static\company-logo.png`). La página lo carga desde `/static/company-logo.png`. Usa ese nombre y extensión `.png`; si el archivo falta, el formulario indica dónde colocarlo.

## Flujo funcional

1. El formulario solicita teléfono y sede y pide consentimiento para usar esos datos, la selfie y el estado de conexión para gestionar la visita. Los datos llegan al panel para revisión.
2. El asesor verifica los datos en el panel. Si algo está mal, puede pedir que se corrijan; el formulario muestra el aviso y permite volver a enviar teléfono y sede. Solo después de aprobarlos se habilita la cámara.
3. Al activar la cámara, el navegador envía al backend fotogramas temporales, que no se guardan, para comprobar cada 800 ms que se detecta un único rostro centrado, a una distancia adecuada, con nitidez suficiente y sin gafas detectadas. El video mantiene el rostro nítido dentro de un óvalo y presenta el fondo desenfocado con tinte rosado. La guía aparece arriba. Cuando la verificación permanece correcta durante tres segundos, la selfie se toma y envía automáticamente; no hay botón de captura. El modelo de gafas (`glasses-detector`, MIT) corre localmente en CPU y descarga sus pesos la primera vez; no envía imágenes a un servicio externo. La selfie final se vuelve a validar antes de guardarse.
4. El panel consulta cambios cada tres segundos. El asesor puede ampliar/guardar la foto, aprobarla para pedir el código o pedir otra selfie.
5. El asesor solicita el código manualmente fuera de este MVP. El visitante escribe los seis dígitos recibidos; el formulario usa el teclado numérico/autocompletado del dispositivo y los envía automáticamente al completar el último dígito. El asesor lo aprueba o lo rechaza desde el panel. Un código rechazado puede ingresarse nuevamente.
6. La página comunica el resultado. El asesor ve presencia activa mientras hay actividad reciente, desconexión tras 60 segundos sin señal, o retiro cuando la persona pulsa **Finalizar visita**.
7. El panel permite bloquear/desbloquear el teléfono, borrar una solicitud y activar avisos sonoros. **Guardar selfie en carpeta** solicita una carpeta al asesor y crea una subcarpeta identificada por el código (o por los últimos cuatro dígitos del teléfono si todavía no hay código); los navegadores sin selector de carpetas ofrecen una descarga normal.

## Alcance y seguridad

- La detección Haar incluida busca rostros, posición y nitidez. Un modelo local separado busca gafas comunes y de sol mediante [`glasses-detector`](https://github.com/mantasu/glasses-detector) (MIT; requiere Python 3.12+ y PyTorch). Como todo detector visual, puede tener falsos positivos o no reconocer algunos casos: el asesor debe revisar la foto. **Las gorras todavía no se detectan automáticamente**; se solicita quitarlas y el asesor las revisa.
- El teléfono enviado en el parámetro URL solo se usa como identificador declarado: todavía no hay firma ni validación criptográfica de la plataforma de verificación que genera ese enlace. No lo uses como prueba de identidad hasta integrar esa verificación.
- El código del trabajador se compara manualmente en el panel; no existe una base de códigos de trabajadores integrada.
- Las fotos, teléfonos y sedes se guardan en `data/` localmente; usa **Eliminar datos** para borrar una solicitud y su foto. Se muestra un aviso de uso y se guarda la fecha de consentimiento. No publiques la aplicación sin HTTPS, controles de acceso operativos, política de retención y revisión de las obligaciones de privacidad laboral aplicables.
- El acceso al panel solo funciona al configurar Telegram y `FLASK_SECRET_KEY`; el código vence a los cinco minutos y se limita a cinco intentos. No hay acceso de administración de prueba.
- El formulario público no muestra un enlace al panel; su URL directa es `/admin`. Esa URL oculta no reemplaza la autenticación por Telegram.
- La presencia se infiere de actividad en la página y no demuestra por sí sola que la persona siga físicamente allí. Si se cierra el navegador, aparecerá desconectada luego de un minuto; puede confirmarse el retiro con el botón.
- En despliegues HTTPS configura `COOKIE_SECURE=1`. El servidor incluido escucha solo en `127.0.0.1`; usa un servidor WSGI y proxy HTTPS para producción.
