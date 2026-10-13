"use client";

const presets = ["10", "25", "50", "100", "250", "500", "1000"];

/** Exact decimal strings pass directly to the server; presets never size trades in-browser. */
export function BuyCapitalControl({
  value,
  onChange,
  label = "Minimum Buy Capital · CAD",
}: {
  value: string;
  onChange: (value: string) => void;
  label?: string;
}) {
  return (
    <div className="buy-capital-control">
      <label>
        {label}
        <input
          aria-label={label}
          inputMode="decimal"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          required
        />
      </label>
      <div className="capital-presets" aria-label={`${label} presets`}>
        {presets.map((amount) => (
          <button
            type="button"
            key={amount}
            aria-pressed={value === amount}
            onClick={() => onChange(amount)}
          >
            C${amount === "1000" ? "1,000" : amount}
          </button>
        ))}
        <span>{presets.includes(value) ? "or enter custom" : "Custom"}</span>
      </div>
    </div>
  );
}
