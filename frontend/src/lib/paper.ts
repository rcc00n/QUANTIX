export type PaperClassification = "VALIDATED" | "RESEARCH";
export type PaperStrategy = {
  schema_version?: string;
  preset: string;
  starting_capital_cad: string;
  trade_amount_cad: string;
  min_buy_capital_cad?: string;
  minimum_profit_cad: string;
  minimum_profit_pct: string;
  threshold_logic: "AND" | "OR";
  reaction_delay_seconds: number;
  maximum_concurrent: number;
  mode: "PRE_FUNDED" | "TRANSFER" | "RESEARCH_SHADOW";
  research_execution_model?: "PRE_FUNDED" | "TRANSFER";
  allowed_exchanges?: string[];
  allowed_assets?: string[];
  [key: string]: unknown;
};