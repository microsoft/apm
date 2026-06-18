import { Show } from "solid-js";

export default function Pagination(props) {
  const totalPages = () => Math.ceil(props.total() / props.pageSize());
  const startItem = () => props.page() * props.pageSize() + 1;
  const endItem = () => Math.min((props.page() + 1) * props.pageSize(), props.total());

  return (
    <Show when={props.total() > 0}>
      <div class="pagination">
        <div class="page-info">
          <span>Show</span>
          <select value={props.pageSize()} onChange={(e) => props.onPageSizeChange(Number(e.target.value))}>
            <option value="25">25</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select>
          <span>per page</span>
          <span>{startItem()}-{endItem()} of {props.total()}</span>
        </div>
        <div class="page-controls">
          <button disabled={props.page() === 0} onClick={() => props.onPageChange(props.page() - 1)}>&lt; Prev</button>
          <span>{props.page() + 1} / {totalPages()}</span>
          <button disabled={props.page() >= totalPages() - 1} onClick={() => props.onPageChange(props.page() + 1)}>Next &gt;</button>
        </div>
      </div>
    </Show>
  );
}
