import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# 1. Configuration
CSV_FILE = 'test_results.csv'
OUTPUT_IMAGE = 'k6_performance_graph.png'

print(f"Loading data from {CSV_FILE}...")

# 2. Load the k6 CSV data
df = pd.read_csv(CSV_FILE, usecols=['metric_name', 'timestamp', 'metric_value'])

# Convert k6 Unix timestamp (seconds) to actual datetime objects for accurate binning
df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')

# 3. Filter and Process Throughput (Requests Per Second)
print("Calculating Requests Per Second (RPS)...")
df_reqs = df[df['metric_name'] == 'http_reqs'].copy()
df_reqs.set_index('datetime', inplace=True)

# FIXED: Changed '1S' to '1s'
rps = df_reqs['metric_value'].resample('1s').count()

# 4. Filter and Process Latency (Using the True Async Metric)
print("Calculating Latency metrics...")

# CRITICAL FIX: We must use the custom metric from the k6 script to measure the actual worker time
df_duration = df[df['metric_name'] == 'async_verification_time'].copy()

# Safety fallback just in case you run an older script without the custom metric
if df_duration.empty:
    print("Async metric not found, falling back to standard http_req_duration.")
    df_duration = df[df['metric_name'] == 'http_req_duration'].copy()

df_duration.set_index('datetime', inplace=True)

def p95(x):
    return np.percentile(x, 95) if len(x) > 0 else np.nan

latency = df_duration['metric_value'].resample('1s').agg(['mean', p95])

if isinstance(latency, pd.Series):
    latency = latency.to_frame()
if latency.shape[1] == 2:
    latency.columns = ['Avg Latency (ms)', 'p95 Latency (ms)']
elif latency.shape[1] == 1:
    latency.columns = ['Avg Latency (ms)']

# This tells Pandas to draw a straight mathematical line across any empty gaps!
latency = latency.interpolate(method='linear')


# ==========================================
# 4.5 THE RELATIVE TIME CONVERSION
# ==========================================
print("Converting timestamps to relative time (seconds)...")

# Find the absolute starting millisecond of the test
start_time = min(rps.index.min(), latency.index.min())

# Subtract the start time from all timestamps and convert to total seconds
rps.index = (rps.index - start_time).total_seconds()
latency.index = (latency.index - start_time).total_seconds()


# 5. Plotting the Graph (Academic/IEEE Style)
print("Generating graph...")
plt.style.use('default') 
fig, ax1 = plt.subplots(figsize=(10, 6), dpi=300) 

color_p95 = 'tab:red'
color_avg = 'tab:orange'

# Updated the X-axis label to reflect the new time format
ax1.set_xlabel('Time Elapsed (Seconds)', fontweight='bold')
ax1.set_ylabel('Response Time (ms)', color='black', fontweight='bold')

line1 = ax1.plot(latency.index, latency['p95 Latency (ms)'], color=color_p95, label='p95 Latency', linewidth=1.5)
line2 = ax1.plot(latency.index, latency['Avg Latency (ms)'], color=color_avg, label='Avg Latency', linewidth=1, alpha=0.7)
ax1.tick_params(axis='y', labelcolor='black')
ax1.grid(True, linestyle='--', alpha=0.6)

ax2 = ax1.twinx()  
color_rps = 'tab:blue'
ax2.set_ylabel('Throughput (Requests / Second)', color=color_rps, fontweight='bold')
line3 = ax2.plot(rps.index, rps, color=color_rps, label='Throughput (RPS)', linewidth=2)
ax2.tick_params(axis='y', labelcolor=color_rps)

ax2.fill_between(rps.index, 0, rps, color=color_rps, alpha=0.1)

lines = line1 + line2 + line3
labels = [str(l.get_label()) for l in lines]
ax1.legend(lines, labels, loc='upper left', frameon=True, shadow=True)

plt.title('ZK-AuthAAS Verification Performance Under Load', fontweight='bold', pad=15)
fig.tight_layout() 

plt.savefig(OUTPUT_IMAGE, format='png', bbox_inches='tight')
print(f"Success! Graph saved as {OUTPUT_IMAGE}")