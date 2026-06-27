import os
import random
import threading
import time
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from datetime import datetime
from werkzeug.middleware.proxy_fix import ProxyFix
from sqlalchemy.orm import DeclarativeBase
import logging

# Configure logging
logging.basicConfig(level=logging.DEBUG)


class Base(DeclarativeBase):
    pass


db = SQLAlchemy(model_class=Base)

# Create the app
app = Flask(__name__)
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(BASE_DIR, 'stocksim.db')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path

app.secret_key = os.environ.get("SESSION_SECRET", "stock_sim_secret_dev")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Configure the database
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Initialize the app with the extension
db.init_app(app)

# Import models after db initialization
from models import Team, Stock, Portfolio, Trade, BankRequest, Announcement, PriceHistory, Loan, GameState, MarketOrder

with app.app_context():
    db.create_all()
    # Create bank team if it doesn't exist
    if not Team.query.filter_by(name="🏦 BANK").first():
        bank_team = Team(name="🏦 BANK", pin="BANK", cash=500000)
        db.session.add(bank_team)
        db.session.commit()
    # Create the singleton game state row if it doesn't exist
    if not GameState.query.first():
        db.session.add(GameState(climate="Neutral"))
        db.session.commit()


def get_game_state():
    """Get the singleton game state row (climate control)"""
    state = GameState.query.first()
    if not state:
        state = GameState(climate="Neutral")
        db.session.add(state)
        db.session.commit()
    return state


# Climate drift ranges, as (min_percent, max_percent) per tick
CLIMATE_DRIFT = {
    "Bull": (0.5, 3.5),
    "Bear": (-3.5, -0.5),
    "Volatile": (-6.0, 6.0),
    "Neutral": (-1.0, 1.0),
}
MARKET_TICK_SECONDS = 25


def market_climate_engine():
    """Background thread that gently drifts all stock prices based on the
    admin-set market climate (Bull/Bear/Volatile/Neutral). Manual admin price
    edits and this engine both write to PriceHistory, so the chart shows both."""
    while True:
        time.sleep(MARKET_TICK_SECONDS)
        try:
            with app.app_context():
                state = get_game_state()
                drift_range = CLIMATE_DRIFT.get(state.climate, CLIMATE_DRIFT["Neutral"])
                stocks = Stock.query.all()
                for stock in stocks:
                    if stock.price <= 0:
                        continue  # bankrupt stocks stay bankrupt
                    drift_pct = random.uniform(*drift_range)
                    old_price = stock.price
                    new_price = round(old_price * (1 + drift_pct / 100), 2)
                    if new_price < 1:
                        new_price = 0.0  # rare climate-driven bankruptcy
                    record_price_change(stock, old_price, new_price)
                    stock.price = new_price
                db.session.commit()
        except Exception as e:
            logging.error(f"Market climate engine error: {e}")


# Only start the background thread once (avoid duplicate threads under the
# Flask debug reloader, which spawns a second process)
if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
    climate_thread = threading.Thread(target=market_climate_engine, daemon=True)
    climate_thread.start()


def get_portfolio(team, stock):
    """Get or create a portfolio entry for a team and stock"""
    pf = Portfolio.query.filter_by(team_id=team.id, stock_id=stock.id).first()
    if not pf:
        pf = Portfolio(team=team, stock=stock, quantity=0)
        db.session.add(pf)
        db.session.flush()
    return pf


def leaderboard_data():
    """Get leaderboard data sorted by total value"""
    return sorted(
        ((t.name, t.total_value()) for t in Team.query.all() if t.name != "🏦 BANK"),
        key=lambda x: x[1],
        reverse=True
    )


def get_bank():
    """Get the bank team"""
    return Team.query.filter_by(name="🏦 BANK").first()


def record_price_change(stock, old_price, new_price):
    """Record a price change in history"""
    if old_price != new_price:
        price_history = PriceHistory(
            stock=stock,
            old_price=old_price,
            new_price=new_price,
            timestamp=datetime.utcnow()
        )
        db.session.add(price_history)


SENTIMENT_WINDOW_MINUTES = 15


def calculate_sentiment(stock=None):
    """Derive organic market sentiment from real buy/sell order flow in the
    last SENTIMENT_WINDOW_MINUTES. Returns dict with label, score (-1..1),
    buy_volume, sell_volume. This is independent of the admin-set climate —
    climate is what admin dictates, sentiment is what traders are actually doing."""
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=SENTIMENT_WINDOW_MINUTES)
    query = MarketOrder.query.filter(MarketOrder.timestamp >= cutoff)
    if stock is not None:
        query = query.filter(MarketOrder.stock_id == stock.id)
    orders = query.all()

    buy_volume = sum(o.qty * o.price for o in orders if o.side == "buy")
    sell_volume = sum(o.qty * o.price for o in orders if o.side == "sell")
    total = buy_volume + sell_volume

    if total == 0:
        score = 0.0
    else:
        score = (buy_volume - sell_volume) / total  # -1 (all selling) to +1 (all buying)

    if score > 0.35:
        label = "Bullish"
    elif score > 0.1:
        label = "Mildly Bullish"
    elif score < -0.35:
        label = "Bearish"
    elif score < -0.1:
        label = "Mildly Bearish"
    else:
        label = "Neutral"

    return {
        "label": label,
        "score": round(score, 3),
        "buy_volume": round(buy_volume, 2),
        "sell_volume": round(sell_volume, 2),
        "order_count": len(orders),
    }


@app.route("/")
def home():
    """Home page with navigation links"""
    return render_template("home.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    """Team login page"""
    if request.method == "POST":
        name = request.form["team"]
        pin = request.form["pin"]
        team = Team.query.filter_by(name=name, pin=pin).first()
        if team:
            session["team"] = name
            return redirect(url_for("team_page", team_name=name))
        return render_template("login.html", error="Invalid team name or PIN")
    return render_template("login.html")


@app.route("/logout")
def logout():
    """Logout and redirect to login"""
    session.pop("team", None)
    return redirect(url_for("login"))


@app.route("/admin", methods=["GET", "POST"])
def admin():
    """Admin panel for managing teams, stocks, and announcements"""
    # Check if admin is authenticated
    if "admin_authenticated" not in session:
        return redirect(url_for("admin_login"))

    msg = ""
    if request.method == "POST":
        f = request.form
        if f["form_type"] == "team":
            try:
                team = Team(name=f["name"], pin=f["pin"], cash=float(f.get("cash", 100000)))
                db.session.add(team)
                db.session.commit()
                msg = "Team added successfully"
            except IntegrityError:
                db.session.rollback()
                msg = "Team name already exists"
        elif f["form_type"] == "stock":
            try:
                stock = Stock(
                    symbol=f["symbol"].upper(),
                    name=f["name"],
                    price=float(f["price"])
                )
                db.session.add(stock)
                db.session.commit()
                msg = "Stock added successfully"
            except IntegrityError:
                db.session.rollback()
                msg = "Stock symbol already exists"
        elif f["form_type"] == "price":
            stock = Stock.query.filter_by(symbol=f["symbol"]).first()
            if stock:
                old_price = stock.price
                new_price = float(f["price"])
                record_price_change(stock, old_price, new_price)
                stock.price = new_price
                db.session.commit()
                msg = f"Price updated for {stock.symbol}"
        elif f["form_type"] == "announce":
            announcement = Announcement(text=f["text"])
            db.session.add(announcement)
            db.session.commit()
            msg = "Announcement posted successfully"
        elif f["form_type"] == "climate":
            state = get_game_state()
            state.climate = f["climate"]
            state.updated_at = datetime.utcnow()
            db.session.commit()
            msg = f"Market climate set to {state.climate}"

    teams = Team.query.filter(Team.name != "🏦 BANK").all()
    stocks = Stock.query.all()
    announcements = Announcement.query.order_by(Announcement.timestamp.desc()).limit(10).all()
    game_state = get_game_state()
    overall_sentiment = calculate_sentiment()

    return render_template("admin.html",
                           msg=msg,
                           teams=teams,
                           stocks=stocks,
                           announcements=announcements,
                           game_state=game_state,
                           sentiment=overall_sentiment)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Admin login page"""
    if request.method == "POST":
        password = request.form["password"]
        # Set your admin password here
        if password == "admin123":  # Change this to your desired password
            session["admin_authenticated"] = True
            return redirect(url_for("admin"))
        return render_template("admin_login.html", error="Invalid admin password")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    """Admin logout"""
    session.pop("admin_authenticated", None)
    return redirect(url_for("home"))


@app.route("/team/<team_name>", methods=["GET", "POST"])
def team_page(team_name):
    """Team dashboard with trading functionality"""
    if "team" not in session or session["team"] != team_name:
        return redirect(url_for("login"))

    team = Team.query.filter_by(name=team_name).first_or_404()
    stocks = Stock.query.all()
    message = ""

    if request.method == "POST":
        act = request.form.get("action")

        if act == "buy_market":
            stock = Stock.query.filter_by(symbol=request.form["symbol"]).first()
            qty = int(request.form["qty"])
            total = abs(stock.price) * qty
            if stock.price == 0.0:
                message = f"{stock.symbol} is bankrupt. You cannot buy its shares."
            elif team.cash >= total:
                pf = get_portfolio(team, stock)
                pf.quantity += qty
                team.cash -= total
                db.session.add(MarketOrder(stock=stock, team=team, side="buy", qty=qty, price=stock.price))
                db.session.commit()
                message = f"Successfully bought {qty} shares of {stock.symbol}"
            else:
                message = "Insufficient cash for this purchase"


        elif act == "sell_market":
            stock = Stock.query.filter_by(symbol=request.form["symbol"]).first()
            qty = int(request.form["qty"])
            if stock.price == 0.0:
                message = f"{stock.symbol} is bankrupt. You cannot sell its shares."
            else:
                pf = get_portfolio(team, stock)
                if pf.quantity >= qty:
                    pf.quantity -= qty
                    team.cash += qty * stock.price
                    db.session.add(MarketOrder(stock=stock, team=team, side="sell", qty=qty, price=stock.price))
                    db.session.commit()
                    message = f"Successfully sold {qty} shares of {stock.symbol}"
                else:
                    message = "Insufficient shares to sell"


        elif act == "sell_to_bank":
            existing_requests = BankRequest.query.filter_by(team_id=team.id, status="pending").count()
            if existing_requests >= 5:
                message = "Bank request limit reached (5 pending requests)"
            else:
                stock = Stock.query.filter_by(symbol=request.form["symbol"]).first()
                qty = int(request.form["qty"])
                pf = get_portfolio(team, stock)
                if stock.price == 0.0:
                    message = f"{stock.symbol} is bankrupt. Cannot sell to bank."
                elif pf.quantity >= qty and qty > 0:
                    bank_req = BankRequest(team=team, stock=stock, qty=qty)
                    db.session.add(bank_req)
                    db.session.commit()
                    message = f"Bank sale request sent for {qty} shares of {stock.symbol}"
                else:
                    message = "Insufficient shares or invalid quantity"

        elif act == "propose_trade":
            existing_trades = Trade.query.filter_by(from_team_id=team.id, status="pending").count()
            if existing_trades >= 5:
                message = "Trade proposal limit reached (5 pending trades)"
            else:
                to_team = Team.query.filter_by(name=request.form["to_team"]).first()
                stock = Stock.query.filter_by(symbol=request.form["symbol"]).first()
                qty = int(request.form["qty"])
                price = float(request.form["price"])
                pf = get_portfolio(team, stock)
                if stock.price == 0.0:
                    message = f"{stock.symbol} is bankrupt. You cannot trade it."
                elif pf.quantity >= qty:
                    trade = Trade(
                        from_team=team,
                        to_team=to_team,
                        stock=stock,
                        qty=qty,
                        price=price
                    )
                    db.session.add(trade)
                    db.session.commit()
                    message = f"Trade proposal sent to {to_team.name}"
                else:
                    message = "Insufficient shares for this trade"

        elif act in ["accept_trade", "reject_trade"]:
            trade = Trade.query.get(int(request.form["trade_id"]))
            if trade.to_team == team and trade.status == "pending":
                if act == "accept_trade":
                    total = trade.qty * trade.price
                    if team.cash >= total:
                        # Check if seller still has the shares
                        from_pf = get_portfolio(trade.from_team, trade.stock)
                        if from_pf.quantity >= trade.qty:
                            team.cash -= total
                            trade.from_team.cash += total
                            get_portfolio(team, trade.stock).quantity += trade.qty
                            from_pf.quantity -= trade.qty
                            trade.status = "accepted"
                            db.session.commit()
                            message = "Trade accepted successfully"
                        else:
                            message = "Seller no longer has enough shares"
                    else:
                        message = "Insufficient cash to accept trade"
                else:
                    trade.status = "rejected"
                    db.session.commit()
                    message = "Trade rejected"

        elif act == "request_loan":
            existing_active = Loan.query.filter_by(team_id=team.id, status="active").count()
            existing_pending = Loan.query.filter_by(team_id=team.id, status="pending").count()
            if existing_active >= 2:
                message = "Loan limit reached (max 2 active loans per team)"
            elif existing_pending >= 1:
                message = "You already have a loan request pending bank approval"
            else:
                amount = float(request.form["amount"])
                if amount <= 0:
                    message = "Loan amount must be greater than 0"
                else:
                    interest_rate = 0.10  # flat 10% interest, due back by event end
                    loan = Loan(
                        team=team,
                        principal=amount,
                        interest_rate=interest_rate,
                        amount_due=round(amount * (1 + interest_rate), 2),
                        status="pending"
                    )
                    db.session.add(loan)
                    db.session.commit()
                    message = f"Loan request for ₹{amount:,.2f} sent to the bank for approval"

        elif act == "repay_loan":
            loan = Loan.query.get(int(request.form["loan_id"]))
            if loan and loan.team_id == team.id and loan.status == "active":
                if team.cash >= loan.amount_due:
                    team.cash -= loan.amount_due
                    loan.status = "repaid"
                    loan.repaid_at = datetime.utcnow()
                    db.session.commit()
                    message = f"Loan repaid in full (₹{loan.amount_due:,.2f})"
                else:
                    message = "Insufficient cash to repay this loan"

    # Get portfolio holdings
    holdings = {}
    for p in team.portfolios:
        if p.quantity > 0:
            holdings[p.stock.symbol] = {
                'quantity': p.quantity,
                'value': p.quantity * p.stock.price,
                'stock': p.stock
            }

    # Get recent announcements
    announcements = Announcement.query.order_by(Announcement.timestamp.desc()).limit(5).all()

    # Get other teams for trading
    all_teams = Team.query.filter(Team.name != team.name, Team.name != "🏦 BANK").all()

    # Get pending trades for this team
    incoming_trades = Trade.query.filter_by(to_team_id=team.id, status="pending").all()
    outgoing_trades = Trade.query.filter_by(from_team_id=team.id, status="pending").all()

    # Get loan data for this team
    active_loans = Loan.query.filter_by(team_id=team.id, status="active").all()
    pending_loans = Loan.query.filter_by(team_id=team.id, status="pending").all()
    past_loans = Loan.query.filter(Loan.team_id == team.id, Loan.status.in_(["repaid", "rejected"])).order_by(
        Loan.issued_at.desc()).limit(5).all()

    game_state = get_game_state()
    overall_sentiment = calculate_sentiment()

    return render_template("team.html",
                           team=team,
                           stocks=stocks,
                           holdings=holdings,
                           announcements=announcements,
                           all_teams=all_teams,
                           incoming_trades=incoming_trades,
                           outgoing_trades=outgoing_trades,
                           active_loans=active_loans,
                           pending_loans=pending_loans,
                           past_loans=past_loans,
                           game_state=game_state,
                           sentiment=overall_sentiment,
                           message=message)


@app.route("/stock/<symbol>")
def stock_detail(symbol):
    """Stock detail page with full price history chart, for trade research"""
    if "team" not in session and "admin_authenticated" not in session:
        return redirect(url_for("login"))

    stock = Stock.query.filter_by(symbol=symbol.upper()).first_or_404()
    history = PriceHistory.query.filter_by(stock_id=stock.id).order_by(PriceHistory.timestamp.asc()).all()

    # Basic stats for the header
    all_prices = [h.new_price for h in history] or [stock.price]
    day_high = max(all_prices + [stock.price])
    day_low = min(all_prices + [stock.price])
    first_price = history[0].old_price if history else stock.price
    net_change = stock.price - first_price
    net_change_pct = (net_change / first_price * 100) if first_price else 0

    sentiment = calculate_sentiment(stock)

    return render_template("stock_detail.html",
                           stock=stock,
                           history=list(reversed(history)),
                           day_high=day_high,
                           day_low=day_low,
                           net_change=net_change,
                           net_change_pct=net_change_pct,
                           sentiment=sentiment)


@app.route("/leaderboard")
def leaderboard():
    """Display team leaderboard"""
    data = leaderboard_data()
    return render_template("leaderboard.html", leaderboard=data)


@app.route("/sebi")
def sebi():
    """SEBI panel for viewing all trades"""
    # Check if SEBI is authenticated
    if "sebi_authenticated" not in session:
        return redirect(url_for("sebi_login"))

    all_trades = Trade.query.order_by(Trade.timestamp.desc()).all()
    return render_template("sebi.html", trades=all_trades)


@app.route("/sebi/login", methods=["GET", "POST"])
def sebi_login():
    """SEBI login page"""
    if request.method == "POST":
        password = request.form["password"]
        # Set your SEBI password here
        if password == "sebi123":  # Change this to your desired password
            session["sebi_authenticated"] = True
            return redirect(url_for("sebi"))
        return render_template("sebi_login.html", error="Invalid SEBI password")
    return render_template("sebi_login.html")


@app.route("/sebi/logout")
def sebi_logout():
    """SEBI logout"""
    session.pop("sebi_authenticated", None)
    return redirect(url_for("home"))


@app.route("/bank", methods=["GET", "POST"])
def bank():
    """Bank panel for managing sale requests and loan requests"""
    # Check if bank is authenticated
    if "bank_authenticated" not in session:
        return redirect(url_for("bank_login"))

    if request.method == "POST":
        req_type = request.form.get("req_type", "sale")

        if req_type == "loan":
            loan_id = int(request.form["loan_id"])
            action = request.form["action"]
            loan = Loan.query.get(loan_id)

            if loan and loan.status == "pending":
                if action == "approve":
                    bank_team = get_bank()
                    if bank_team.cash >= loan.principal:
                        loan.team.cash += loan.principal
                        bank_team.cash -= loan.principal
                        loan.status = "active"
                        db.session.commit()
                    else:
                        loan.status = "rejected"
                        db.session.commit()
                else:
                    loan.status = "rejected"
                    db.session.commit()
        else:
            req_id = int(request.form["request_id"])
            action = request.form["action"]
            bank_req = BankRequest.query.get(req_id)

            if bank_req and bank_req.status == "pending":
                if action == "approve":
                    team = bank_req.team
                    stock = bank_req.stock
                    qty = bank_req.qty

                    # Check if team still has the shares
                    pf = get_portfolio(team, stock)
                    if pf.quantity >= qty:
                        # Transfer shares from team to bank
                        pf.quantity -= qty
                        team.cash += qty * stock.price

                        # Add to bank portfolio
                        bank_team = get_bank()
                        bank_pf = get_portfolio(bank_team, stock)
                        bank_pf.quantity += qty
                        bank_team.cash -= qty * stock.price

                        bank_req.status = "approved"
                        db.session.commit()
                    else:
                        bank_req.status = "rejected"
                        db.session.commit()
                else:
                    bank_req.status = "rejected"
                    db.session.commit()

    pending_requests = BankRequest.query.filter_by(status="pending").all()
    pending_loans = Loan.query.filter_by(status="pending").order_by(Loan.issued_at.asc()).all()
    active_loans = Loan.query.filter_by(status="active").all()
    bank_team = get_bank()
    return render_template("bank.html", requests=pending_requests, pending_loans=pending_loans,
                           active_loans=active_loans, bank_team=bank_team)


@app.route("/bank/login", methods=["GET", "POST"])
def bank_login():
    """Bank login page"""
    if request.method == "POST":
        password = request.form["password"]
        # Set your bank password here
        if password == "bank123":  # Change this to your desired password
            session["bank_authenticated"] = True
            return redirect(url_for("bank"))
        return render_template("bank_login.html", error="Invalid bank password")
    return render_template("bank_login.html")


@app.route("/bank/logout")
def bank_logout():
    """Bank logout"""
    session.pop("bank_authenticated", None)
    return redirect(url_for("home"))


@app.route("/api/stocks")
def api_stocks():
    """API endpoint for stock data with price change information"""
    stocks = Stock.query.all()
    stock_data = []

    for stock in stocks:
        # Get latest price change
        latest_change = PriceHistory.query.filter_by(stock_id=stock.id).order_by(PriceHistory.timestamp.desc()).first()

        price_change = 0
        change_percent = 0
        if latest_change:
            price_change = latest_change.new_price - latest_change.old_price
            if latest_change.old_price > 0:
                change_percent = (price_change / latest_change.old_price) * 100

        stock_data.append({
            'id': stock.id,
            'symbol': stock.symbol,
            'name': stock.name,
            'price': stock.price,
            'price_change': price_change,
            'change_percent': change_percent,
            'last_updated': latest_change.timestamp.isoformat() if latest_change else None
        })

    return jsonify(stock_data)


@app.route("/api/price-history/<symbol>")
def api_price_history(symbol):
    """API endpoint for stock price history"""
    stock = Stock.query.filter_by(symbol=symbol).first_or_404()
    history = PriceHistory.query.filter_by(stock_id=stock.id).order_by(PriceHistory.timestamp.desc()).limit(20).all()

    history_data = []
    for h in history:
        history_data.append({
            'timestamp': h.timestamp.isoformat(),
            'old_price': h.old_price,
            'new_price': h.new_price,
            'change': h.new_price - h.old_price
        })

    return jsonify(history_data)


@app.route("/api/game-state")
def api_game_state():
    """API endpoint for current market climate"""
    state = get_game_state()
    return jsonify({"climate": state.climate, "updated_at": state.updated_at.isoformat()})


@app.route("/api/sentiment")
def api_sentiment():
    """API endpoint for overall market sentiment, derived from real order flow"""
    return jsonify(calculate_sentiment())


@app.route("/api/sentiment/<symbol>")
def api_sentiment_stock(symbol):
    """API endpoint for per-stock sentiment, derived from real order flow"""
    stock = Stock.query.filter_by(symbol=symbol.upper()).first_or_404()
    return jsonify(calculate_sentiment(stock))


@app.route("/api/announcements")
def api_announcements():
    """API endpoint for announcements"""
    announcements = Announcement.query.order_by(Announcement.timestamp.desc()).limit(10).all()

    announcement_data = []
    for announcement in announcements:
        announcement_data.append({
            'id': announcement.id,
            'text': announcement.text,
            'timestamp': announcement.timestamp.isoformat()
        })

    return jsonify(announcement_data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)