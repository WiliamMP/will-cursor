# will-cursor

Controle o mouse do Windows com a mão, pela webcam. Usa o [MediaPipe Hand Landmarker](https://ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker) para achar a mão e o `pyautogui` para mexer o cursor.

## Gestos

Uma mão:

- Dedo indicador: move o cursor
- Pinça polegar + indicador: clique (segurar = arrastar)
- Pinça polegar + médio: clique direito
- Indicador e médio esticados, outros dobrados: rolagem, subindo e descendo a mão
- Punho fechado parado: `Enter`
- Palma aberta varrendo para o lado: troca de área de trabalho virtual
- Punho varrendo para o lado: joga a janela para o próximo monitor

Duas mãos:

- Bater palmas: abre o Claude Code
- Pinça nas duas mãos, abrindo ou fechando: zoom
- Dois punhos segurados: pausa e retoma (pausado, só esse gesto funciona)

## Como rodar

Python 3 e uma webcam.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python cursor.py
```

O modelo `hand_landmarker.task` é baixado na primeira execução. `Ctrl+C` no terminal fecha (ou `q`/`Esc` na janela da câmera, com o HUD desligado).

Os ajustes (câmera, margem, suavização, sensibilidade dos gestos, sons, HUD) ficam em constantes no começo do `cursor.py`.

## Testes

```bash
.venv/Scripts/python cursor.py --test
```

## Licença

[MIT](LICENSE)
