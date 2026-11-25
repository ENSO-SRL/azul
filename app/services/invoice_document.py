"""Colina del Sol invoice, rendered from one immutable, cent-based snapshot."""
from __future__ import annotations

import base64
from html import escape
from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Image, Paragraph, Spacer, Table, TableStyle

LOGO = Path(__file__).resolve().parents[2] / 'public' / 'invoice-logo.png'
ISSUER = {'name': 'Colina del Sol, SRL', 'rnc': '133-11765-7',
          'address': 'Calle José López, Los Prados', 'city': 'Santo Domingo, República Dominicana',
          'phone': '809-480-5571', 'support': 'soporte@iamatlas.do'}


def money(cents: int, currency: str) -> str:
    return f"{'US$' if currency == 'USD' else 'RD$'} {cents / 100:,.2f}"


def render_html(d: dict, *, embed_logo: bool = False) -> str:
    """Table-based, inline CSS for mail clients; all dynamic values escaped."""
    e = lambda value: escape(str(value or ''), quote=True)
    m = lambda key: money(d[key], d['currency'])
    issuer = d['issuer']
    logo = ('data:image/jpeg;base64,' + base64.b64encode(LOGO.read_bytes()).decode()) if embed_logo else 'cid:invoice_logo'
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Factura {e(d['number'])}</title>
<style>td,th {{overflow-wrap:anywhere;word-break:break-word}}
@media only screen and (max-width:500px) {{.invoice-shell {{padding:24px 14px!important}}
.invoice-items td,.invoice-items th {{padding:8px 3px!important;font-size:11px!important}}
.issuer {{font-size:12px!important}}}}</style></head>
<body style="margin:0;background:#f4f4f4;font:14px Arial,sans-serif;color:#151515">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 10px">
<table role="presentation" class="invoice-shell" width="100%" cellspacing="0" cellpadding="0" style="width:100%;max-width:680px;table-layout:fixed;box-sizing:border-box;background:white;padding:36px 28px">
<tr><td><img src="{logo}" alt="iamAtlas" width="90" height="90" style="display:block"></td>
<td class="issuer" align="right" style="line-height:1.45">{e(issuer['name'])}<br>RNC: {e(issuer['rnc'])}<br>{e(issuer['address'])}<br>{e(issuer['city'])}<br>Tel: {e(issuer['phone'])}</td></tr>
<tr><td style="padding:34px 0;font-size:28px;font-weight:bold">FACTURA</td>
<td align="right"><strong>{m('total')}</strong><br><span style="color:#18734b">PAGADA</span></td></tr>
<tr><td style="line-height:1.65;padding:0 8px 28px 0">Número: {e(d['number'])}<br>Emisión: {e(d['date'])}<br>Vencimiento: {e(d['date'])}</td>
<td style="line-height:1.65;padding-bottom:28px"><strong>Facturar a:</strong><br>{e(d['customer_name'])}<br>{e(d['customer_email'])}</td></tr>
<tr><td colspan="2" style="border-top:1px solid #bbb;padding-top:26px">
<table class="invoice-items" width="100%" cellspacing="0" cellpadding="8" style="table-layout:fixed;border-collapse:collapse;font-size:13px">
<tr style="border-bottom:2px solid #111"><th width="42%" align="left">Descripción</th><th width="12%">Cant.</th><th width="23%" align="right">Precio unitario</th><th width="23%" align="right">Monto</th></tr>
<tr><td>{e(d['description'])}</td><td align="center">1</td><td align="right">{m('subtotal')}</td><td align="right">{m('subtotal')}</td></tr></table></td></tr>
<tr><td colspan="2" style="padding:28px 8px"><table width="100%" cellpadding="5" cellspacing="0">
<tr><td>Subtotal:</td><td align="right">{m('subtotal')}</td></tr>
<tr><td>ITBIS{e(d['tax_label'])}:</td><td align="right">{m('itbis')}</td></tr>
<tr><td><strong>Total:</strong></td><td align="right"><strong>{m('total')}</strong></td></tr>
<tr><td><strong>Monto pendiente:</strong></td><td align="right"><strong>{m('balance_due')}</strong></td></tr></table></td></tr>
<tr><td colspan="2" style="line-height:1.7;padding:14px 8px"><strong>Método de pago:</strong> {e(d['payment_method'])}<br>
<strong>Referencia del pago:</strong> {e(d['payment_id'])}</td></tr>
<tr><td colspan="2" style="padding:24px 8px 8px;line-height:1.6">Gracias por utilizar iamAtlas.<br>Adjuntamos tu factura en PDF.<br>Soporte: <a href="mailto:{e(issuer['support'])}" style="color:#151515">{e(issuer['support'])}</a></td></tr>
</table></td></tr></table></body></html>'''


def render_pdf(d: dict) -> bytes:
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=letter, leftMargin=50, rightMargin=50,
                            topMargin=42, bottomMargin=42, title='Factura '+d['number'], author=d['issuer']['name'])
    styles = getSampleStyleSheet()
    normal = ParagraphStyle('InvoiceBody', parent=styles['Normal'], fontName='Helvetica', fontSize=10, leading=14)
    right = ParagraphStyle('InvoiceRight', parent=normal, alignment=TA_RIGHT)
    p = lambda s, style=normal: Paragraph(s, style)
    e = lambda v: escape(str(v or ''))
    m = lambda key: money(d[key], d['currency'])
    issuer = d['issuer']
    head = Table([[Image(str(LOGO), width=80, height=80), p('<br/>'.join(map(e, [issuer['name'],
        'RNC: '+issuer['rnc'], issuer['address'], issuer['city'], 'Tel: '+issuer['phone']])), right)]], colWidths=[155,357])
    head.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),0)]))
    story = [head, Spacer(1,28)]
    title = Table([[p('<b>FACTURA</b>', styles['Heading1']), p(f'<b>{m("total")}</b><br/>PAGADA',right)]],colWidths=[256,256])
    story += [title, Spacer(1,26)]
    details = Table([[p(f'Número de factura: {e(d["number"])}<br/>Fecha de emisión: {e(d["date"])}<br/>Fecha de vencimiento: {e(d["date"])}'),
        p(f'<b>Facturar a:</b><br/>{e(d["customer_name"])}<br/>{e(d["customer_email"])}')]],colWidths=[290,222])
    details.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP')]))
    story += [details, Spacer(1,38)]
    items = Table([[p('<b>Descripción</b>'),p('<b>Cantidad</b>'),p('<b>Precio unitario</b>',right),p('<b>Monto</b>',right)],
        [p(e(d['description'])),p('1',right),p(m('subtotal'),right),p(m('subtotal'),right)]], colWidths=[228,70,107,107])
    items.setStyle(TableStyle([('LINEABOVE',(0,0),(-1,0),.5,colors.grey),('LINEBELOW',(0,0),(-1,0),1,colors.black),
        ('TOPPADDING',(0,0),(-1,-1),10),('BOTTOMPADDING',(0,0),(-1,-1),10),('VALIGN',(0,0),(-1,-1),'TOP')]))
    totals = Table([[p(label),p(value,right)] for label,value in [
        ('Subtotal:',m('subtotal')),('ITBIS'+e(d['tax_label'])+':',m('itbis')),
        ('<b>Total:</b>','<b>'+m('total')+'</b>'),('<b>Monto pendiente:</b>','<b>'+m('balance_due')+'</b>')]],colWidths=[350,162])
    totals.setStyle(TableStyle([('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
    story += [items,Spacer(1,28),totals,Spacer(1,26),
        p('<b>Método de pago:</b> '+e(d['payment_method'])),p('Referencia: '+e(d['payment_id'])),Spacer(1,26),
        p('Gracias por utilizar iamAtlas.<br/>Soporte: '+e(issuer['support']))]
    doc.build(story)
    return output.getvalue()


def render_text(d: dict) -> str:
    return (f"Factura {d['number']} - {d['issuer']['name']}\nRNC: {d['issuer']['rnc']}\n"
        f"Cliente: {d['customer_name']}\nEmisión: {d['date']}\n{d['description']}\n"
        f"Subtotal: {money(d['subtotal'],d['currency'])}\nITBIS: {money(d['itbis'],d['currency'])}\n"
        f"Total: {money(d['total'],d['currency'])}\nMonto pendiente: {money(d['balance_due'],d['currency'])}\n"
        f"Pago: {d['payment_method']}\nAdjuntamos la factura en PDF.\nSoporte: {d['issuer']['support']}")
