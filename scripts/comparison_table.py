import json
from prettytable import PrettyTable

def create_comparison_table():
    # Define headers and metrics

    headers = ['val_sdf_loss/dataloader_idx_0',
            'val_tau_loss/dataloader_idx_0',
            'val_reconstruction_reconstruction_loss/dataloader_idx_0',
            'val_smoothness_diff/dataloader_idx_1',
            'val_tau_loss/dataloader_idx_2', 
            'val_ort_std/dataloader_idx_2', 
            'val_orig_std/dataloader_idx_2', 
            'val_tau_std/dataloader_idx_2']

    import pandas as pd

    table_headers = [
        'MSE_sdf', 'MSE_tau', 'MSE_reconstruction', 'smoothness_diff',
        'MSE_tau', 'ort_std', 'orig_std', 'tau_std'
    ]

    latex_table_headers = [
        r"$MSE_{sdf}$", r"$MSE_{\tau}$*",
        r"$MSE_{\chi}$", r"Smooth",
        r"$MSE_{\tau}$", r"orto",
        r"$STD(Z_{\bar{\tau}})$", r"$STD(Z_{\bar{\tau}})$"
    ]

    metrics_smaller_is_better = [
        'MSE_sdf', 'MSE_tau', 'MSE_reconstruction',
        'smoothness_diff', 'MSE_tau', 'ort_std', 'orig_std'
    ]

    metrics_larger_is_better = ['tau_std']

    results_path = 'src/metrics.json'

    with open(results_path, 'r') as file:
        results = json.load(file)

        table = PrettyTable()
        table.field_names = ["Model"] + table_headers

        # Find best values for each metric
        best_values = {}
        for header in headers:
            values = [metrics.get(header, float('inf')) for metrics in results.values()]
            if header in [headers[i] for i, h in enumerate(table_headers) if h in metrics_smaller_is_better]:
                best_values[header] = min(values)
            else:
                best_values[header] = max(values)

        # Create lists to store data for pandas DataFrame
        df_data = []
        
        for model_name, metrics in results.items():
            row = [model_name]
            row_data = {'Model': model_name}
            
            for i, header in enumerate(headers):
                value = metrics.get(header, "N/A")
                if value != "N/A":
                    # Add * if this is the best value
                    is_best = abs(value - best_values[header]) < 1e-10
                    formatted_value = f"\033[91m{value:.3g}\033[0m" if is_best else f"{value:.3g}"
                    row.append(formatted_value)
                    # Store raw value in row_data for DataFrame
                    row_data[table_headers[i]] = value
                else:
                    row.append("N/A")
                    row_data[table_headers[i]] = None
                    
            table.add_row(row)
            df_data.append(row_data)

        print(table)
        
        # Create pandas DataFrame
        df = pd.DataFrame(df_data)
        
        # Replace column headers with LaTeX versions
        df.columns = ['Model'] + latex_table_headers
        
        # Format values and highlight best metrics
        def format_value(x, header_idx):
            if pd.isnull(x):
                return 'N/A'
            header = table_headers[header_idx-1] if header_idx > 0 else None
            if header:
                is_best = abs(x - best_values[headers[header_idx-1]]) < 1e-10
                if is_best:
                    return f'\\textbf{{{x:.3g}}}'
            return f'{x:.3g}'
        
        formatted_df = df.copy()
        for i in range(len(df.columns)):
            if i > 0:  # Skip Model column
                formatted_df.iloc[:,i] = df.iloc[:,i].apply(lambda x: format_value(x, i))
        
        # Convert DataFrame to LaTeX table
        latex_table = formatted_df.to_latex(
            index=False,
            caption='Comparison of Model Metrics',
            label='tab:model_metrics',
            escape=False
        )
        
        # Add table styling
        latex_table = latex_table.replace('\\begin{table}', '\\begin{table}[htbp]')
        latex_table = latex_table.replace('\\toprule', '\\hline')
        latex_table = latex_table.replace('\\midrule', '\\hline')
        latex_table = latex_table.replace('\\bottomrule', '\\hline')
        
        print("\nLaTeX Table:")
        print(latex_table)

if __name__ == "__main__":
    create_comparison_table()