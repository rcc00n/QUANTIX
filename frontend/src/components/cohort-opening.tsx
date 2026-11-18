import { Exact } from "@/components/terminal-ui";
import { assetLabel, type LabOpeningAllocation } from "@/lib/paper";

export function CohortOpening({
  allocation,
  proposalId,
}: {
  allocation: LabOpeningAllocation;
  proposalId: string;
}) {
  return (
    <details className="evidence-details">
      <summary>Common opening funding</summary>
      <p className="terminal-description break-all">Funding proposal {proposalId}</p>
      <dl className="settings-list">
        <div>
          <dt>Target purchase size</dt>
          <dd>
            <Exact value={allocation.target_trade_cad} unit="CAD" />
          </dd>
        </div>
        <div>
          <dt>Opening reserve</dt>
          <dd>
            <Exact value={allocation.reserve_cad} unit="CAD" />
          </dd>
        </div>
        <div>
          <dt>Unallocated capital</dt>
          <dd>
            <Exact value={allocation.unallocated_cad} unit="CAD" />
          </dd>
        </div>
      </dl>
      <div className="table-wrap">
        <table className="terminal-table" aria-label="Opening inventory by venue and asset">
          <thead>
            <tr>
              <th scope="col">Venue</th>
              <th scope="col">Asset</th>
              <th scope="col">Native quantity</th>
              <th scope="col">Opening CAD value</th>
              <th scope="col">CAD per unit</th>
            </tr>
          </thead>
          <tbody>
            {allocation.allocations.map((row) => (
              <tr key={`${row.exchange}:${row.asset}`}>
                <td>{row.exchange}</td>
                <td>{assetLabel(row.asset)}</td>
                <td>
                  <Exact value={row.quantity} />
                </td>
                <td>
                  <Exact value={row.cad_value} unit="CAD" />
                </td>
                <td>
                  <Exact value={row.cad_rate} unit="CAD" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table className="terminal-table" aria-label="Opening route capacity by purchase size">
          <thead>
            <tr>
              <th scope="col">Minimum purchase size</th>
              <th scope="col">Routes considered</th>
              <th scope="col">Funded alternatives</th>
              <th scope="col">Funded together</th>
              <th scope="col">Positive after costs</th>
            </tr>
          </thead>
          <tbody>
            {allocation.coverage_by_size.map((row) => (
              <tr key={row.trade_amount_cad}>
                <th scope="row">
                  <Exact value={row.trade_amount_cad} unit="CAD" />
                </th>
                <td>{row.considered_routes}</td>
                <td>{row.funded_alternative_routes}</td>
                <td>{row.simultaneous_funded_routes}</td>
                <td>{row.profitable_funded_routes}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="terminal-footnote">
        Alternative routes can share balances. Funded-together counts budget both legs for each
        route. Profit figures are opening projections using pinned evidence.
      </p>
      {allocation.funded_routes.length > 0 && (
        <div className="table-wrap">
          <table className="terminal-table" aria-label="Routes funded at opening">
            <thead>
              <tr>
                <th scope="col">Asset</th>
                <th scope="col">Buy venue</th>
                <th scope="col">Sell venue</th>
                <th scope="col">Supported purchase</th>
                <th scope="col">Projected net CAD</th>
              </tr>
            </thead>
            <tbody>
              {allocation.funded_routes.map((row) => (
                <tr key={row.route_id}>
                  <td>{assetLabel(row.base_asset)}</td>
                  <td>{row.buy_exchange}</td>
                  <td>{row.sell_exchange}</td>
                  <td>
                    <Exact value={row.maximum_funded_acquisition_cad} unit="CAD" />
                  </td>
                  <td>
                    <Exact value={row.net_profit_cad} unit="CAD" color />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}
