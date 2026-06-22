import React, { useState } from 'react';
import { CreditCard, Trash2, Star, Plus } from 'lucide-react';
import './PaymentMethods.css';

export default function PaymentMethods() {
  const [cardNumber, setCardNumber] = useState('');
  const [cardHolder, setCardHolder] = useState('');
  const [expiry, setExpiry] = useState('');
  const [cvv, setCvv] = useState('');
  const [saveCard, setSaveCard] = useState(true);

  return (
    <div className="payment-container" style={{ fontFamily: 'Inter, sans-serif' }}>
      <div className="header-section">
        <div className="icon-wrapper">
          <CreditCard size={20} color="#DA007C" />
        </div>
        <div>
          <h2 className="title">Métodos de pago</h2>
          <p className="subtitle">Agrega y administra tus tarjetas</p>
        </div>
      </div>

      <div className="layout-grid">
        {/* Left Column: Form & Visual Card */}
        <div className="form-section">
          {/* Visual Card Mockup */}
          <div className="visual-card">
            <CreditCard size={28} color="white" className="card-icon" />
            <div className="card-details">
              <div className="card-number">
                {cardNumber || '•••• •••• •••• ••••'}
              </div>
              <div className="card-footer">
                <div className="card-holder">
                  {cardHolder.toUpperCase() || 'NOMBRE DEL TITULAR'}
                </div>
                <div className="card-expiry">
                  {expiry || 'MM/AA'}
                </div>
              </div>
            </div>
            <div className="card-bg-decoration"></div>
          </div>

          {/* Form */}
          <form className="payment-form" onSubmit={(e: React.FormEvent) => e.preventDefault()}>
            <div className="form-group">
              <label>Número de tarjeta</label>
              <div className="input-with-icon">
                <CreditCard size={16} color="#aaa" className="input-icon" />
                <input 
                  type="text" 
                  className="signup-input with-icon" 
                  placeholder="0000 0000 0000 0000"
                  value={cardNumber}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCardNumber(e.target.value)}
                />
              </div>
            </div>

            <div className="form-group">
              <label>Nombre del titular</label>
              <div className="input-with-icon">
                <svg className="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
                <input 
                  type="text" 
                  className="signup-input with-icon" 
                  placeholder="Como aparece en la tarjeta"
                  value={cardHolder}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCardHolder(e.target.value)}
                />
              </div>
            </div>

            <div className="form-row">
              <div className="form-group half">
                <label>Vencimiento</label>
                <div className="input-with-icon">
                  <svg className="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line></svg>
                  <input 
                    type="text" 
                    className="signup-input with-icon" 
                    placeholder="MM/AA"
                    value={expiry}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setExpiry(e.target.value)}
                  />
                </div>
              </div>
              <div className="form-group half">
                <label>CVV</label>
                <div className="input-with-icon">
                  <svg className="input-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#aaa" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect><path d="M7 11V7a5 5 0 0 1 10 0v4"></path></svg>
                  <input 
                    type="text" 
                    className="signup-input with-icon" 
                    placeholder="123"
                    value={cvv}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) => setCvv(e.target.value)}
                  />
                </div>
              </div>
            </div>

            <div className="checkbox-group">
              <label className="checkbox-label">
                <input 
                  type="checkbox" 
                  checked={saveCard}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSaveCard(e.target.checked)}
                />
                <span className="checkmark"></span>
                Guardar esta tarjeta para futuros pagos
              </label>
            </div>

            <button type="submit" className="signup-btn-filled">
              <Plus size={18} />
              Agregar tarjeta
            </button>
          </form>
        </div>

        {/* Right Column: Saved Cards */}
        <div className="saved-cards-section">
          <div className="saved-cards-header">
            <h3 className="section-title">Tarjetas guardadas</h3>
            <span className="count-badge">2</span>
          </div>

          <div className="saved-card-item">
            <div className="card-brand mastercard">M</div>
            <div className="card-info">
              <div className="card-title">Mastercard <span className="dots">••••</span> 1190</div>
              <div className="card-expiry-text">Vence 03/26</div>
            </div>
            <div className="card-actions">
              <button className="icon-btn"><Star size={16} color="#aaa" /></button>
              <button className="icon-btn delete-btn"><Trash2 size={16} /></button>
            </div>
          </div>

          <div className="saved-card-item active">
            <div className="card-brand visa">V</div>
            <div className="card-info">
              <div className="card-title">Visa <span className="dots">••••</span> 4821</div>
              <div className="status-pill">Predeterminada</div>
              <div className="card-expiry-text">Vence 09/27</div>
            </div>
            <div className="card-actions">
              <button className="icon-btn"><Star size={16} color="#78C609" fill="#78C609" /></button>
              <button className="icon-btn delete-btn"><Trash2 size={16} /></button>
            </div>
          </div>

        </div>
      </div>

    </div>
  );
}
