PRD v3: pivot a tendencia + privacy-first, cierra bloqueante BodyM

- Pivot del producto a tendencia/composicion relativa: delta-silueta,
  ratios y trayectoria, nunca valor absoluto de % grasa. Cierra
  dependencia del dataset BodyM (CC BY-NC 4.0) y elimina riesgo FDA
  como medical device en un solo movimiento.
- Privacy-first como wedge: procesamiento 100% on-device (MediaPipe
  + segmentacion de silueta). La foto cruda nunca sale del iPhone.
- Fase 0 re-secuenciada (8 semanas):
  W1-2 validar motor con datos publicos opt-in de r/progresspics
  (gate r >= 0.7), W3-4 prototipo iOS privacy-first, W5-6 Concierge
  con 5-8 cercanos (no 20), W7-8 gate willingness-to-pay.
- Metric de precision cambia de MAE absoluto vs DEXA a correlacion
  Pearson entre delta-silueta y delta-peso/DEXA reportado.
