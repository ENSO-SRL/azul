import asyncio
from app.infrastructure.database import async_session
from sqlalchemy import select
from app.infrastructure.models import PaymentModel, TransactionModel

async def main():
    async with async_session() as session:
        res = await session.execute(
            select(PaymentModel).order_by(PaymentModel.created_at.desc()).limit(10)
        )
        payments = res.scalars().all()
        print("=== ULTIMOS 10 PAGOS ===")
        for p in payments:
            print(f"Payment ID: {p.id} | OrderID (custom): {p.order_id} | AzulOrderId: {p.azul_order_id} | Status: {p.status}")

        res_tx = await session.execute(
            select(TransactionModel).order_by(TransactionModel.created_at.desc()).limit(10)
        )
        txs = res_tx.scalars().all()
        for t in txs:
            print(f"Tx ID: {t.id} | ResponseCode: {t.response_code} | IsoCode: {t.iso_code} | PaymentID: {t.payment_id}")

if __name__ == "__main__":
    asyncio.run(main())
