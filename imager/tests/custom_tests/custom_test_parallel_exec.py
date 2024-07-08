import requests
import concurrent.futures

url = 'http://localhost:8000/api/generate-image'
payload = {
    'image_path': './files/test_image.jpg',
    'group_name': 'dota_icons',
    'insertion_format': 'crop',
    'alpha_channel': 30,
    'noise_level': 40,
    'cell_size': 60,
    'result_size': 120
}
headers = {
    'Content-Type': 'application/json'
}

def send_request():
    response = requests.post(url, json=payload, headers=headers)
    return response.status_code, response.text

num_requests = 5

with concurrent.futures.ThreadPoolExecutor(max_workers=num_requests) as executor:
    futures = [executor.submit(send_request) for _ in range(num_requests)]
    for future in concurrent.futures.as_completed(futures):
        status_code, response_text = future.result()
        print(f'Status Code: {status_code}, Response: {response_text}')
