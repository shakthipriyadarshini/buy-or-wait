import { useEffect, useState } from "react";
import {
  Wallet,
  ShieldCheck,
  TrendingUp,
  CalendarDays,
  ArrowRight,
  CheckCircle2,
  AlertTriangle,
  Sparkles,
  CreditCard,
  ChevronRight,
} from "lucide-react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
} from "recharts";
import "./App.css";

// Point this at wherever `python api.py` is running.
// In production Flask serves this bundle from the same origin as the API,
// so an empty base ("" -> "/api/...") is correct and avoids hardcoding a
// deployed URL. In `npm run dev` the Vite dev server is on :5173 while the
// API is on :5000, so vite.config.js proxies /api there — which means the
// same relative path works in both cases. VITE_API_BASE overrides for the
// split-deployment case (frontend and API on different hosts).
const API_BASE = import.meta.env.VITE_API_BASE ?? "";

// Illustrative only — the backend computes the real 90-day balance walk
// internally (engine/forecast.py) but doesn't expose the day-by-day series
// over the API yet, so this chart stays sample data until that's added.
const sampleForecastData = [
  { date: "Day 0", balance: 100 },
  { date: "Day 15", balance: 88 },
  { date: "Day 30", balance: 96 },
  { date: "Day 45", balance: 78 },
  { date: "Day 60", balance: 84 },
  { date: "Day 75", balance: 90 },
  { date: "Day 90", balance: 82 },
];

const STATUS_META = {
  affordable_now: { label: "AFFORDABLE NOW", icon: CheckCircle2, tone: "good" },
  affordable_with_plan: { label: "AFFORDABLE WITH PLAN", icon: CheckCircle2, tone: "good" },
  affordable_later: { label: "AFFORDABLE LATER", icon: AlertTriangle, tone: "warn" },
  not_affordable: { label: "NOT AFFORDABLE", icon: AlertTriangle, tone: "bad" },
};

function formatCurrency(value, currency) {
  try {
    return new Intl.NumberFormat("en-IN", {
      style: "currency",
      currency: currency || "USD",
      maximumFractionDigits: 0,
    }).format(value);
  } catch {
    // Intl throws on an unrecognized currency code — fall back to a plain number
    return `${currency || ""} ${Math.round(value).toLocaleString()}`;
  }
}

function parsePaymentPlan(planStr) {
  if (!planStr || planStr === "none") return [];
  return planStr.split("|").map((chunk) => {
    const [date, amount] = chunk.split(":");
    return { date, amount: Number(amount) };
  });
}

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

function addDaysISO(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

function App() {
  const [users, setUsers] = useState([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [amount, setAmount] = useState("");
  const [item, setItem] = useState("");
  const [decision, setDecision] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${API_BASE}/api/users`)
      .then((r) => r.json())
      .then((data) => {
        setUsers(data);
        if (data.length > 0) setSelectedUserId(data[0].user_id);
      })
      .catch(() => setError("Couldn't reach the backend. Is `python api.py` running?"));
  }, []);

  const currentUser = users.find((u) => u.user_id === selectedUserId);
  const requestedAmount = Number(amount) || 0;

  async function handleCheck() {
    if (!selectedUserId || requestedAmount <= 0) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${API_BASE}/api/requests/evaluate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: selectedUserId,
          requested_amount: requestedAmount,
          request_date: todayISO(),
          desired_completion_date: addDaysISO(30),
          allows_partial_payment: true,
          request_type: "purchase",
          request_text: item || "Can I afford this?",
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Request failed");
      setDecision(data);
    } catch (e) {
      setError(e.message);
      setDecision(null);
    } finally {
      setLoading(false);
    }
  }

  const currency = decision?.profile?.home_currency || currentUser?.home_currency || "USD";
  const statusMeta = decision ? STATUS_META[decision.affordability_status] : null;
  const StatusIcon = statusMeta?.icon || CheckCircle2;
  const paymentRows = decision ? parsePaymentPlan(decision.payment_plan) : [];
  const spendingChanges =
    decision && decision.spending_changes_needed !== "none"
      ? decision.spending_changes_needed.split("|")
      : [];

  return (
    <div className="app">
      {/* Sidebar */}
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">
            <Wallet size={22} />
          </div>

          <div>
            <h2>Buy or Wait?</h2>
            <span>Financial Agent</span>
          </div>
        </div>

        <nav>
          <button className="nav-item active">
            <Sparkles size={18} />
            Affordability
          </button>

          <button className="nav-item">
            <TrendingUp size={18} />
            Forecast
          </button>

          <button className="nav-item">
            <CreditCard size={18} />
            Payment plans
          </button>
        </nav>

        <div className="sidebar-bottom">
          <div className="secure">
            <ShieldCheck size={18} />
            <div>
              <strong>Financial data</strong>
              <span>Protected & private</span>
            </div>
          </div>
        </div>
      </aside>

      {/* Main */}
      <main className="main">
        <header className="topbar">
          <div>
            <p className="eyebrow">PERSONAL FINANCE</p>
            <h1>Can I afford this?</h1>
            <p className="subtitle">
              I'll check your current balance, upcoming expenses and
              90-day financial safety.
            </p>
          </div>

          <div className="date-pill">
            <CalendarDays size={16} />
            {new Date().toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" })}
          </div>
        </header>

        {/* Purchase input */}
        <section className="purchase-card">
          <div className="section-label">
            <Sparkles size={16} />
            What are you planning to buy?
          </div>

          <div className="purchase-inputs">
            <div className="input-group item-input">
              <label>User</label>
              <select
                value={selectedUserId}
                onChange={(e) => setSelectedUserId(e.target.value)}
              >
                {users.map((u) => (
                  <option key={u.user_id} value={u.user_id}>
                    {u.user_id}
                  </option>
                ))}
              </select>
            </div>

            <div className="input-group item-input">
              <label>Purchase</label>
              <input
                value={item}
                onChange={(e) => setItem(e.target.value)}
                placeholder="e.g. Laptop"
              />
            </div>

            <div className="input-group amount-input">
              <label>Amount ({currentUser?.home_currency || "..."})</label>

              <div className="amount-wrapper">
                <span>{currentUser?.home_currency || ""}</span>

                <input
                  type="number"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                />
              </div>
            </div>

            <button
              className="check-button"
              onClick={handleCheck}
              disabled={loading || !selectedUserId || requestedAmount <= 0}
            >
              {loading ? "Checking..." : "Check affordability"}
              <ArrowRight size={18} />
            </button>
          </div>

          {error && <p style={{ color: "#b3402a", marginTop: 10 }}>{error}</p>}
        </section>

        {/* Financial snapshot */}
        {currentUser && (
          <section className="stats-grid">
            <div className="stat-card">
              <div className="stat-icon">
                <Wallet size={20} />
              </div>

              <div>
                <span>Current balance</span>
                <strong>{formatCurrency(currentUser.current_available_balance, currency)}</strong>
              </div>
            </div>

            <div className="stat-card highlighted">
              <div className="stat-icon">
                <ShieldCheck size={20} />
              </div>

              <div>
                <span>Safe to spend today</span>
                <strong>
                  {decision ? formatCurrency(decision.amount_safe_to_pay, currency) : "—"}
                </strong>
              </div>
            </div>

            <div className="stat-card">
              <div className="stat-icon">
                <ShieldCheck size={20} />
              </div>

              <div>
                <span>Minimum balance</span>
                <strong>{formatCurrency(currentUser.minimum_balance_to_keep, currency)}</strong>
              </div>
            </div>
          </section>
        )}

        {decision && (
          <>
            {/* Decision */}
            <section className="decision-card">
              <div className="decision-header">
                <div
                  className="status-icon"
                  style={
                    statusMeta?.tone === "bad"
                      ? { background: "#f6d5d0", color: "#8a3323" }
                      : statusMeta?.tone === "warn"
                      ? { background: "#f5e3ae", color: "#7a5c1f" }
                      : undefined
                  }
                >
                  <StatusIcon size={28} />
                </div>

                <div>
                  <span className="status-label">{statusMeta?.label}</span>

                  <h2>
                    {decision.affordability_status === "not_affordable"
                      ? `You shouldn't buy ${item || "this"} right now`
                      : decision.affordability_status === "affordable_later"
                      ? `Wait to buy ${item || "this"}`
                      : `You can afford ${item || "this purchase"}`}
                  </h2>

                  <p>{decision.decision_explanation}</p>
                </div>
              </div>

              <div className="decision-details">
                <div>
                  <span>Safe amount today</span>
                  <strong>{formatCurrency(decision.amount_safe_to_pay, currency)}</strong>
                </div>

                <div>
                  <span>Recommended method</span>
                  <strong className="method">
                    {decision.recommended_payment_method.replace("_", " ")}
                  </strong>
                </div>

                <div>
                  <span>Full payment possible</span>
                  <strong>{decision.earliest_date_for_full_payment || "Not within 90 days"}</strong>
                </div>
              </div>
            </section>

            {/* Forecast */}
            <section className="content-card">
              <div className="card-heading">
                <div>
                  <span className="section-label">
                    <TrendingUp size={16} />
                    90-DAY FORECAST
                  </span>

                  <h2>Projected balance</h2>
                </div>

                <div className="legend">
                  <span className="legend-dot" />
                  Sample shape — not this request's real forecast yet
                </div>
              </div>

              <div className="chart">
                <ResponsiveContainer width="100%" height={300}>
                  <AreaChart data={sampleForecastData}>
                    <defs>
                      <linearGradient id="balanceGradient" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopOpacity={0.25} />
                        <stop offset="100%" stopOpacity={0} />
                      </linearGradient>
                    </defs>

                    <CartesianGrid strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="date" axisLine={false} tickLine={false} />
                    <YAxis axisLine={false} tickLine={false} width={55} />
                    <Tooltip />
                    <ReferenceLine y={100} strokeDasharray="5 5" label="Minimum balance" />
                    <Area type="monotone" dataKey="balance" strokeWidth={3} fill="url(#balanceGradient)" />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </section>

            {/* Explanation */}
            <section className="bottom-grid">
              <div className="content-card explanation-card">
                <div className="card-heading">
                  <div>
                    <span className="section-label">
                      <Sparkles size={16} />
                      AGENT EXPLANATION
                    </span>

                    <h2>Why this decision?</h2>
                  </div>
                </div>

                <div className="reason">
                  <CheckCircle2 size={19} />
                  <span>{decision.decision_explanation}</span>
                </div>

                {spendingChanges.map((change) => (
                  <div className="reason warning" key={change}>
                    <AlertTriangle size={19} />
                    <span>{change}</span>
                  </div>
                ))}
              </div>

              <div className="content-card plan-card">
                <div className="card-heading">
                  <div>
                    <span className="section-label">
                      <CreditCard size={16} />
                      PAYMENT PLAN
                    </span>

                    <h2>Suggested schedule</h2>
                  </div>
                </div>

                {paymentRows.length === 0 && <p>No payment scheduled.</p>}

                {paymentRows.map((row, i) => {
                  const d = new Date(row.date);
                  const month = d.toLocaleDateString("en-US", { month: "short" }).toUpperCase();
                  const day = d.getDate();
                  return (
                    <div className="payment-row" key={row.date + i}>
                      <div className="payment-date">
                        <span>{month}</span>
                        <strong>{day}</strong>
                      </div>

                      <div>
                        <strong>{i === 0 ? "First payment" : `Payment ${i + 1}`}</strong>
                        <span>{row.date}</span>
                      </div>

                      <strong>{formatCurrency(row.amount, currency)}</strong>
                    </div>
                  );
                })}
              </div>
            </section>
          </>
        )}

        <footer>
          <ShieldCheck size={14} />
          Decisions are calculated using your financial data and
          a 90-day safety forecast.
        </footer>
      </main>
    </div>
  );
}

export default App;
