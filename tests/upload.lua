-- upload.lua
local api_key = "YOUR_API_KEY_HERE"  -- Replace with your actual API key

-- Check if the API key has been set
if api_key == "YOUR_API_KEY_HERE" then
    error("API key has not been set. Please replace 'YOUR_API_KEY_HERE' with your actual API key.")
end

wrk.method = "POST"
wrk.body = "--boundary\r\n" ..
           "Content-Disposition: form-data; name=\"file\"; filename=\"test.txt\"\r\n" ..
           "Content-Type: text/plain\r\n\r\n" ..
           "This is a test file content.\r\n" ..
           "--boundary--\r\n"
wrk.headers["Content-Type"] = "multipart/form-data; boundary=boundary"
wrk.headers["x-api-key"] = api_key
