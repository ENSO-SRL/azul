"""Create demo artifacts only; never connects to a database/payment/mail service."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.services.invoice_document import ISSUER,render_html,render_pdf

parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
args.output.mkdir(parents=True,exist_ok=True)
d={'number':'INV-ATLAS-2026-DEMO','payment_id':'DEMO-SIN-COBRO-REAL','issuer':ISSUER,
   'date':'25/11/2026','customer_name':'Cliente de demostración','customer_email':'cliente@example.com',
   'description':'Suscripción iamAtlas - noviembre 2026 (demostración)',
   'currency':'DOP','subtotal':50000,'itbis':9000,'total':59000,'balance_due':0,
   'tax_label':' (18%)','payment_method':'Tarjeta terminada en 4242 (prueba)'}
(args.output/'factura-atlas.html').write_text(render_html(d,embed_logo=True),encoding='utf-8')
(args.output/'factura-atlas.pdf').write_bytes(render_pdf(d))
print('Created demo invoice HTML/PDF in',args.output)
