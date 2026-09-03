import csv
import sys
from typing import List, Dict, Any

def read_csv_data(csv_file: str) -> tuple[List[str], List[List[str]]]:
    """讀取 CSV 檔案並返回標題和資料"""
    with open(csv_file, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = [row for row in reader]
    return headers, rows

def calculate_column_widths(headers: List[str], rows: List[List[str]]) -> List[int]:
    """計算每個欄位所需的最大寬度"""
    widths = [len(header) for header in headers]
    
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(str(cell)))
    
    return widths

def format_row(row: List[str], widths: List[int], separator: str = " | ") -> str:
    """格式化單行資料，使其對齊"""
    formatted_cells = []
    for i, cell in enumerate(row):
        if i < len(widths):
            # 數字欄位右對齊，其他左對齊
            if cell.replace('.', '').replace('-', '').isdigit():
                formatted_cells.append(str(cell).rjust(widths[i]))
            else:
                formatted_cells.append(str(cell).ljust(widths[i]))
    
    return separator.join(formatted_cells)

def csv_to_aligned_txt(csv_file: str, output_file: str = None):
    """將 CSV 轉換為對齊的 TXT 格式"""
    try:
        # 讀取 CSV 資料
        headers, rows = read_csv_data(csv_file)
        
        # 計算欄位寬度
        widths = calculate_column_widths(headers, rows)
        
        # 準備輸出內容
        output_lines = []
        
        # 格式化標題
        header_line = format_row(headers, widths)
        output_lines.append(header_line)
        
        # 添加分隔線
        separator_line = "-" * len(header_line)
        output_lines.append(separator_line)
        
        # 格式化資料行
        for row in rows:
            formatted_row = format_row(row, widths)
            output_lines.append(formatted_row)
        
        # 輸出結果
        output_content = "\n".join(output_lines)
        
        if output_file:
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(output_content)
            print(f"已將對齊的資料寫入: {output_file}")
        else:
            print(output_content)
            
    except FileNotFoundError:
        print(f"錯誤: 找不到檔案 {csv_file}")
    except Exception as e:
        print(f"處理檔案時發生錯誤: {e}")

def main():
    """主程式"""
    if len(sys.argv) < 2:
        print("使用方法: python csv_to_aligned_txt.py <input.csv> [output.txt]")
        print("範例: python csv_to_aligned_txt.py logs/merged_stats.csv logs/aligned_stats.txt")
        return
    
    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    
    csv_to_aligned_txt(input_file, output_file)

if __name__ == "__main__":
    main()