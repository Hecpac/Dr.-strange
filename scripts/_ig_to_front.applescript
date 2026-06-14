-- Bring the Chrome window whose active tab is Instagram to the front.
tell application "Google Chrome"
	activate
	set found to false
	set report to ""
	set winIdx to 0
	repeat with w in windows
		set winIdx to winIdx + 1
		set tabIdx to 0
		repeat with t in tabs of w
			set tabIdx to tabIdx + 1
			set u to URL of t
			if u contains "instagram.com" then
				set active tab index of w to tabIdx
				set index of w to 1
				set found to true
				set report to "RAISED window " & winIdx & " tab " & tabIdx & " :: " & u
				exit repeat
			end if
		end repeat
		if found then exit repeat
	end repeat
	if not found then
		set report to "NO_IG_TAB_FOUND windows=" & (count of windows)
	end if
end tell
return report
