import { InboxOutlined } from '@ant-design/icons';
import { Table } from 'antd';
import type { TableProps } from 'antd';

const numberFormat = new Intl.NumberFormat('zh-CN');

export default function WorkbenchTable<Row extends object>({
  emptyMessage = '暂无数据', pagination, locale, ...props
}: TableProps<Row> & { emptyMessage?: string }) {
  return <Table<Row> {...props} pagination={pagination === false ? false : {
    hideOnSinglePage: true,
    showSizeChanger: false,
    showTotal: (total, [from, to]) => `${numberFormat.format(from)}–${numberFormat.format(to)} 项，共 ${numberFormat.format(total)} 项`,
    ...pagination,
  }} locale={{
    emptyText: <div className="table-empty">
      <span className="table-empty-icon" aria-hidden="true"><InboxOutlined /></span>
      <span>{emptyMessage}</span>
    </div>,
    ...locale,
  }} />;
}
