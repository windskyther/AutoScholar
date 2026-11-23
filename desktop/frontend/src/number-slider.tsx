import { InputNumber, Slider } from 'antd';

// A Form-compatible single value keeps keyboard entry and the slider synchronized.
export default function NumberSlider({ value, onChange, min, max, precision = 0, step = 1, disabled, id, label }: {
  value?: number | null; onChange?: (value: number | null) => void;
  min: number; max: number; precision?: number; step?: number; disabled?: boolean; id?: string; label?: string;
}) {
  return <div className="number-slider">
    <Slider ariaLabelForHandle={label ? `${label}滑条` : '调整数值'} value={value ?? min}
      min={min} max={max} step={step} disabled={disabled} onChange={onChange} />
    <InputNumber id={id} value={value} onChange={onChange} min={min} max={max}
      step={step} precision={precision} disabled={disabled} style={{ width: '100%' }} />
  </div>;
}
