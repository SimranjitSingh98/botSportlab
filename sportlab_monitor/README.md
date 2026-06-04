# SportLab Monitor — Deploy su Render.com (gratis)

## Passi

1. Crea account su https://github.com e carica questa cartella come nuovo repository
   - New repository → nome: sportlab-monitor → crea
   - Carica i 3 file: monitor.py, requirements.txt, render.yaml

2. Vai su https://render.com → crea account gratis (login con GitHub)

3. Dashboard → "New" → "Background Worker"
   - Collega il tuo repository GitHub
   - Render legge render.yaml automaticamente

4. Aggiungi la variabile d'ambiente:
   - Key: TELEGRAM_TOKEN
   - Value: il tuo token (rigenerato con /revoke su @BotFather)

5. Deploy → il bot parte e gira per sempre

## Note
- Render free tier: 750 ore/mese gratis = gira tutto il mese senza problemi
- I log sono visibili nella dashboard Render in tempo reale
- Se il bot va down, Render lo riavvia automaticamente
