# PulsePPG · IoT + Big Data (ITQ)

Demo académico para la clase de **IoT y Big Data** del ITQ.

El proyecto une las dos disciplinas en un solo flujo: una **Raspberry Pi 5** con un sensor **MAX30102** captura la señal PPG (fotopletismografía) de una persona, genera un **PIN de 6 dígitos** y lo muestra en un LCD. La Pi publica las muestras crudas y el PIN por **MQTT** a **ThingsBoard**, que actúa como plataforma IoT: recibe los datos, los almacena como series temporales y los expone mediante una **API REST**. Una **web en FastAPI** permite al usuario teclear el PIN, consulta la telemetría del dispositivo en ThingsBoard, recupera las muestras crudas y ejecuta un análisis de señal que devuelve una etiqueta (`stress`, `no_stress`, `low_quality`), un score y métricas. El resultado es efímero: no se persiste, se recalcula en cada consulta.

La parte **IoT** está en el borde: el sensor, la Pi, MQTT con QoS 1 y el Access Token del dispositivo. La parte **Big Data** está en la plataforma: almacenamiento de series temporales, consultas por rango de tiempo y por valor, y una capa analítica que procesa las muestras bajo demanda. La web no reinventa la infraestructura: la orquesta.

El objetivo académico no es construir una plataforma desde cero, sino demostrar que se puede **integrar una plataforma IoT existente con una capa de análisis propia**, y que el flujo completo —captura, transporte, almacenamiento, consulta y análisis— es trazable y reproducible.
