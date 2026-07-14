@echo off
echo Adding WSL2 port proxy for port 8777...
netsh interface portproxy add v4tov4 listenport=8777 listenaddress=0.0.0.0 connectport=8777 connectaddress=172.24.120.89
echo.
echo Adding firewall rule WSL2_8777...
netsh advfirewall firewall add rule name=WSL2_8777 dir=in action=allow protocol=TCP localport=8777
echo.
echo Done! Press any key to exit.
pause >nul
