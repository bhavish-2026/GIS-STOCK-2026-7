from app import db
from datetime import datetime
from sqlalchemy import func


class Team(db.Model):
    """Team model for participating teams"""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    pin = db.Column(db.String(20), nullable=False)
    cash = db.Column(db.Float, default=100000.0)

    # Relationships
    portfolios = db.relationship("Portfolio", back_populates="team", cascade="all, delete-orphan")
    bank_requests = db.relationship("BankRequest", back_populates="team", cascade="all, delete-orphan")
    from_trades = db.relationship("Trade", foreign_keys="Trade.from_team_id", back_populates="from_team")
    to_trades = db.relationship("Trade", foreign_keys="Trade.to_team_id", back_populates="to_team")
    loans = db.relationship("Loan", back_populates="team", cascade="all, delete-orphan")

    def outstanding_debt(self):
        """Total amount still owed on active loans (principal + interest)"""
        return round(sum(l.amount_due for l in self.loans if l.status == "active"), 2)

    def total_value(self):
        """Calculate total portfolio value including cash, minus any outstanding loan debt"""
        portfolio_value = sum(p.quantity * p.stock.price for p in self.portfolios if p.quantity > 0)
        return round(self.cash + portfolio_value - self.outstanding_debt(), 2)

    def __repr__(self):
        return f'<Team {self.name}>'


class Stock(db.Model):
    """Stock model for tradeable stocks"""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(10), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Float, nullable=False)

    # Relationships
    portfolios = db.relationship("Portfolio", back_populates="stock")
    trades = db.relationship("Trade", back_populates="stock")
    bank_requests = db.relationship("BankRequest", back_populates="stock")
    price_history = db.relationship("PriceHistory", back_populates="stock", cascade="all, delete-orphan")

    def __repr__(self):
        return f'<Stock {self.symbol}>'


class Portfolio(db.Model):
    """Portfolio model for team stock holdings"""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    stock_id = db.Column(db.Integer, db.ForeignKey("stock.id"), nullable=False)
    quantity = db.Column(db.Integer, default=0)

    # Relationships
    team = db.relationship("Team", back_populates="portfolios")
    stock = db.relationship("Stock", back_populates="portfolios")

    # Unique constraint
    __table_args__ = (db.UniqueConstraint('team_id', 'stock_id'),)

    def __repr__(self):
        return f'<Portfolio {self.team.name} - {self.stock.symbol}: {self.quantity}>'


class Trade(db.Model):
    """Trade model for team-to-team transactions"""
    id = db.Column(db.Integer, primary_key=True)
    from_team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    to_team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    stock_id = db.Column(db.Integer, db.ForeignKey("stock.id"), nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default="pending")  # pending, accepted, rejected
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    from_team = db.relationship("Team", foreign_keys=[from_team_id], back_populates="from_trades")
    to_team = db.relationship("Team", foreign_keys=[to_team_id], back_populates="to_trades")
    stock = db.relationship("Stock", back_populates="trades")

    def total_value(self):
        """Calculate total trade value"""
        return self.qty * self.price

    def __repr__(self):
        return f'<Trade {self.from_team.name} -> {self.to_team.name}: {self.qty} {self.stock.symbol}>'


class BankRequest(db.Model):
    """Bank request model for selling to bank"""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    stock_id = db.Column(db.Integer, db.ForeignKey("stock.id"), nullable=False)
    qty = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="pending")  # pending, approved, rejected
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    team = db.relationship("Team", back_populates="bank_requests")
    stock = db.relationship("Stock", back_populates="bank_requests")

    def __repr__(self):
        return f'<BankRequest {self.team.name}: {self.qty} {self.stock.symbol}>'


class Announcement(db.Model):
    """Announcement model for admin messages"""
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<Announcement {self.text[:50]}...>'


class PriceHistory(db.Model):
    """Price history model for tracking stock price changes"""
    id = db.Column(db.Integer, primary_key=True)
    stock_id = db.Column(db.Integer, db.ForeignKey("stock.id"), nullable=False)
    old_price = db.Column(db.Float, nullable=False)
    new_price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    # Relationships
    stock = db.relationship("Stock", back_populates="price_history")

    def change_amount(self):
        """Calculate price change amount"""
        return self.new_price - self.old_price

    def change_percent(self):
        """Calculate price change percentage"""
        if self.old_price > 0:
            return ((self.new_price - self.old_price) / self.old_price) * 100
        return 0

    def __repr__(self):
        return f'<PriceHistory {self.stock.symbol}: {self.old_price} -> {self.new_price}>'


class Loan(db.Model):
    """Loan model for bank-issued credit to teams"""
    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    principal = db.Column(db.Float, nullable=False)
    interest_rate = db.Column(db.Float, default=0.10)  # 10% flat interest by default
    amount_due = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default="pending")  # pending, active, repaid, rejected
    issued_at = db.Column(db.DateTime, default=datetime.utcnow)
    repaid_at = db.Column(db.DateTime, nullable=True)

    # Relationships
    team = db.relationship("Team", back_populates="loans")

    def __repr__(self):
        return f'<Loan {self.team.name}: principal {self.principal}, due {self.amount_due}, {self.status}>'


class GameState(db.Model):
    """Singleton-style model tracking the overall market climate/mood"""
    id = db.Column(db.Integer, primary_key=True)
    climate = db.Column(db.String(20), default="Neutral")  # Bull, Bear, Volatile, Neutral
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<GameState climate={self.climate}>'


class MarketOrder(db.Model):
    """Logs every market buy/sell so we can derive organic market sentiment
    (separate from the admin-set climate) from real order flow."""
    id = db.Column(db.Integer, primary_key=True)
    stock_id = db.Column(db.Integer, db.ForeignKey("stock.id"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("team.id"), nullable=False)
    side = db.Column(db.String(4), nullable=False)  # 'buy' or 'sell'
    qty = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    stock = db.relationship("Stock")
    team = db.relationship("Team")

    def __repr__(self):
        return f'<MarketOrder {self.side} {self.qty} {self.stock.symbol}>'